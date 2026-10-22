"""The core of the ``diff`` read (section 0.8, contract v1.4), shared by both modes: ``ctx.caps.git.diff`` per linked
repo, combined into ``{sources, files, unified, truncated}``.

* **head** is the branch head the reader observes (the ``source_sha`` a verdict would sign, D58); **base** and its
  ``base_source`` follow section 0.8 (H3b1: ``local_ref``, else ``first_parent``; a declared ``default_branch`` that
  is missing is ``observe.unavailable``), with the ``base..head`` commit list (display only). Every object is
  hash-checked by the reader, so the diff is built from exactly those commits.
* ``repo`` must name a repo in the ticket's ``links.repos`` (``not_found`` otherwise); without it every linked repo
  is diffed, in name order, and ``unified`` is their patches one after the other.
* ``unified`` is cut at ``max_bytes`` on a line boundary (``truncated``); it is the patch bytes decoded as UTF-8
  (an undecodable byte becomes U+FFFD: frames carry text).
* A declared ``remote`` that differs from the observed identity is ``repo.remote_mismatch`` (F7).

Two phases: :func:`diff_plan` reads the store (the host runs it on the workspace worker thread) and
:func:`diff_run` reads git (the host runs it off that thread, :mod:`orch.host.diff`); :func:`diff_result` is both.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.ops.errors import OrchError

__all__ = ["RepoJob", "RepoReadError", "diff_plan", "diff_result", "diff_run"]


class RepoReadError(OrchError):
    """A reader refusal for one repo: ``repo`` and the reader's own ``reason`` kept apart, so a host can show a
    path-free message to a caller who may not see the repo's path (the reason may name it)."""

    def __init__(self, code: str, repo: str, reason: str, *, retryable: bool | None = None) -> None:
        super().__init__(code, f"{repo}: {reason}"[:200], retryable=retryable)
        self.repo, self.reason = repo, reason


@dataclass(frozen=True)
class RepoJob:
    """One linked repo to diff, resolved from the store (the reader phase needs no store)."""

    name: str
    branch: str
    path: Path
    default_branch: str | None
    remote: str | None
    #: H6c, from the store: the ticket's ``branch.pushed`` entries of this repo name (newest first), the caller's own
    #: still-counting code approvals' source entries (newest first), and the host-pinned bases of this ticket
    pushes: tuple[Any, ...] = field(default=(), compare=False)
    approvals: tuple[Any, ...] = field(default=(), compare=False)
    pins: tuple[Any, ...] = field(default=(), compare=False)


PUSHES_MAX = 100


def approvals_of(store: Any, view: Any, person: str | None) -> list[dict[str, str]]:
    """``{repo, sha}`` of every source entry signed by a ``code`` approval of ``person`` (the caller's own: never
    another person's, never a verdict), newest decision first. Fails closed: ``person`` must still be a member, and a
    decision counts only while it is not voided and its signing device is the person's own, present and not revoked
    (nor listed as revoked on a settled ticket). The walk stops at the newest decision that fails this, so a revoked
    approval is never replaced by an older one. A push that raised the gate's generation leaves an approval usable:
    "what changed since I approved" is asked exactly then. ``person`` ``None`` (no-person) has none."""
    if person is None:
        return []
    core = store.state._core
    ws, t = core.ws, core.tickets[view.uid]
    gate = t.gates.get("code")
    if gate is None or person not in ws.members:
        return []
    out: list[dict[str, str]] = []
    for d in reversed(gate.decisions):
        if d.kind != "approve" or d.person != person or not d.source_sha:
            continue
        dev = ws.devices.get(d.signer)  # a hub key ("hub:...") is no device: fails closed
        if (
            d.voided
            or d.id in gate.revoked_flag
            or dev is None
            or dev.removed
            or dev.revoked is not None
            or dev.person != person
        ):
            break
        out += [{"repo": x["repo"], "sha": x["sha"]} for x in d.source_sha]
    return out[:64]


def pushes_of(store: Any, view: Any, name: str) -> list[dict[str, Any]]:
    """The ticket's ``branch.pushed`` events for the linked repo ``name``, newest first (at most
    :data:`PUSHES_MAX`), as the reader's authorization input."""
    out = []
    for e in reversed(store.events(view.uid)):
        if e["type"] == "branch.pushed" and e.get("repo_name") == name:
            b = e.get("before")
            out.append(
                {
                    "repo": e["repo_id"],
                    "ref": e["ref"],
                    "sha": e["sha"],
                    "before": None
                    if not isinstance(b, dict)
                    else {"repo": b["repo_id"], "ref": b["ref"], "sha": b["sha"]},
                }
            )
            if len(out) >= PUSHES_MAX:
                break
    return out


def diff_plan(
    store: Any,
    view: Any,
    caps: Any,
    *,
    repo: str | None = None,
    person: str | None = None,
    since: bool = False,
    extended: bool = False,
    pins: Any = (),
) -> list[RepoJob]:
    """The store part: which repos, on which branch, where. ``not_found`` for a repo the ticket does not link;
    ``observe.unavailable`` for one without a branch or a working copy. ``since`` (``"my_approval"``) loads the
    approvals of ``person``; ``extended`` (``head``/``base``) the ticket's pushes; ``pins``: ``{repo_id: sha}``
    host-pinned bases of this ticket (host mode)."""
    links = view.fields["links"]
    repos = store.state.workspace.repos
    names = sorted(links["repos"])
    if repo is not None:
        if repo not in names:
            raise OrchError("not_found", f"{repo} is not linked to this ticket")
        names = [repo]
    jobs = []
    for name in names:
        branch = links["branches"].get(name)
        path = caps.git.repo_path(store.layout, repos, name)
        if not branch:
            raise OrchError("observe.unavailable", f"{name}: the ticket names no branch")
        if path is None:
            raise OrchError("observe.unavailable", f"{name}: no working copy in settings.repos")
        spec = repos[name]
        jobs.append(
            RepoJob(
                name,
                branch,
                path,
                spec.get("default_branch"),
                spec.get("remote"),
                tuple(pushes_of(store, view, name)) if extended else (),
                tuple(approvals_of(store, view, person)) if since else (),
                tuple({"repo": r, "sha": v} for r, v in sorted(dict(pins).items())),
            )
        )
    return jobs


def diff_run(
    caps: Any,
    jobs: list[RepoJob],
    *,
    context: int = 3,
    max_bytes: int = 262144,
    budget: float | None = None,
    since: bool = False,
    head: str | None = None,
    base: str | None = None,
    pin_base: Callable[[RepoJob, str, str], None] | None = None,
) -> dict[str, Any]:
    """The reader part: ``caps.git.diff`` per job, combined. ``budget``: seconds for every repo together (host
    mode: each reader call gets what is left; none left is ``observe.unavailable``). ``since`` / ``head`` / ``base``:
    the H6c arguments (one repo, checked by :func:`check_ext`). ``pin_base(job, repo_id, sha)``: host mode records the
    first merge base it sees for a repo identity of this ticket (``host_pinned``)."""
    from orch.gitread.errors import Busy, NotFound, RemoteMismatch, Unavailable
    from orch.ops.caps import CapabilityDenied

    deadline = None if budget is None else time.monotonic() + budget
    sources: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    truncated = False
    for job in jobs:
        name = job.name
        extra: dict[str, Any] = {}
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RepoReadError("observe.unavailable", name, f"the diff took longer than {budget:g} s")
            extra["wall"] = left
        ext: dict[str, Any] = {}
        if since or head is not None or base is not None or job.pins or pin_base is not None:
            ext = {"name": name}
            for key, val in (
                ("since", True if since else None),
                ("head", head),
                ("base", base),
                ("pins", list(job.pins) or None),
                ("approvals", list(job.approvals) or None),
                ("pushes", list(job.pushes) or None),
            ):
                if val is not None:
                    ext[key] = val
        try:
            got = caps.git.diff(
                job.path,
                name=name,
                branch=job.branch,
                default_branch=job.default_branch,
                remote=job.remote,
                context=context,
                max_bytes=max_bytes,
                **({"ext": ext} if ext else {}),
                **extra,
            )
        except NotFound as e:
            raise RepoReadError("not_found", name, e.reason) from None
        except Busy as e:
            raise RepoReadError("quota.host", name, e.reason, retryable=True) from None
        except RemoteMismatch as e:
            raise RepoReadError("repo.remote_mismatch", name, e.reason) from None
        except (Unavailable, CapabilityDenied) as e:
            raise RepoReadError("observe.unavailable", name, str(getattr(e, "reason", str(e)))) from None
        if pin_base is not None and got.get("observed_base") and not any(True for _ in _pinned(job, got)):
            from orch.gitread.client import identity_of

            pin_base(job, identity_of(got["origin_urls"], name), got["observed_base"])
        sources.append(
            {
                "repo": name,
                "branch": job.branch,
                "base_sha": got["base_sha"],
                "head_sha": got["head_sha"],
                "base_source": got["base_source"],
                "base_moved": got["base_moved"],
                "commits": got["commits"],
                "commits_more": got["commits_more"],
            }
        )
        files += [{"repo": name, **f} for f in got["files"]]
        chunks.append(base64.b64decode(got["patch"]))
        truncated = truncated or bool(got["truncated"])
    patch = b"".join(chunks)
    if len(patch) > max_bytes:
        nl = patch.rfind(b"\n", 0, max_bytes)
        patch = patch[: nl + 1] if nl >= 0 else b""
        truncated = True
    return {
        "sources": sources,
        "files": files,
        "unified": patch.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


def _pinned(job: RepoJob, got: dict[str, Any]) -> Any:
    """The pins of this job that already name the repo identity the reader observed."""
    from orch import canon
    from orch.gitread.client import identity_of

    ident = identity_of(got["origin_urls"], job.name)
    return (p for p in job.pins if canon.same_repo_identity(p["repo"], ident))


def check_ext(since: Any, head: Any, base: Any, repo: str | None) -> bool:
    """The H6c arguments (§0.8.5): ``since`` is only ``"my_approval"`` and excludes ``head`` and ``base``; ``head`` and
    ``base`` are object ids (hex, 40 or 64) and need ``repo``. Returns whether ``since`` was given."""
    if since is not None and since != "my_approval":
        raise OrchError("invalid.input", 'since must be "my_approval"')
    for k, v in (("head", head), ("base", base)):
        if v is not None and (type(v) is not str or len(v) not in (40, 64) or not set(v) <= set("0123456789abcdef")):
            raise OrchError("invalid.input", f"{k} must be a full lowercase object id")
    if since is not None and (head is not None or base is not None):
        raise OrchError("invalid.input", "since cannot be combined with head or base")
    if (head is not None or base is not None) and repo is None:
        raise OrchError("invalid.input", "head and base need repo")
    return since is not None


def check_args(context: Any, max_bytes: Any) -> None:
    if type(context) is not int or not 0 <= context <= 10:
        raise OrchError("invalid.input", "context must be 0..10")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
        raise OrchError("invalid.input", "max_bytes must be 1024..1048576")


def diff_result(
    caps: Any,
    store: Any,
    view: Any,
    *,
    repo: str | None = None,
    context: int = 3,
    max_bytes: int = 262144,
    since: str | None = None,
    head: str | None = None,
    base: str | None = None,
    person: str | None = None,
) -> dict[str, Any]:
    """Both phases in one call (direct mode, and the reference the host's split is tested against). ``person``: the
    caller's person, for ``since="my_approval"``."""
    check_args(context, max_bytes)
    use_since = check_ext(since, head, base, repo)
    jobs = diff_plan(
        store, view, caps, repo=repo, person=person, since=use_since, extended=head is not None or base is not None
    )
    return diff_run(caps, jobs, context=context, max_bytes=max_bytes, since=use_since, head=head, base=base)
