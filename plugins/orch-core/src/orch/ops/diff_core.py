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
from dataclasses import dataclass
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


def diff_plan(store: Any, view: Any, caps: Any, *, repo: str | None = None) -> list[RepoJob]:
    """The store part: which repos, on which branch, where. ``not_found`` for a repo the ticket does not link;
    ``observe.unavailable`` for one without a branch or a working copy."""
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
        path = caps.git.repo_path(store.root, repos, name)
        if not branch:
            raise OrchError("observe.unavailable", f"{name}: the ticket names no branch")
        if path is None:
            raise OrchError("observe.unavailable", f"{name}: no working copy in settings.repos")
        spec = repos[name]
        jobs.append(RepoJob(name, branch, path, spec.get("default_branch"), spec.get("remote")))
    return jobs


def diff_run(
    caps: Any, jobs: list[RepoJob], *, context: int = 3, max_bytes: int = 262144, budget: float | None = None
) -> dict[str, Any]:
    """The reader part: ``caps.git.diff`` per job, combined. ``budget``: seconds for every repo together (host
    mode: each reader call gets what is left; none left is ``observe.unavailable``)."""
    from orch.gitread.errors import Busy, RemoteMismatch, Unavailable
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
        try:
            got = caps.git.diff(
                job.path,
                name=name,
                branch=job.branch,
                default_branch=job.default_branch,
                remote=job.remote,
                context=context,
                max_bytes=max_bytes,
                **extra,
            )
        except Busy as e:
            raise RepoReadError("quota.host", name, e.reason, retryable=True) from None
        except RemoteMismatch as e:
            raise RepoReadError("repo.remote_mismatch", name, e.reason) from None
        except (Unavailable, CapabilityDenied) as e:
            raise RepoReadError("observe.unavailable", name, str(getattr(e, "reason", str(e)))) from None
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


def check_args(context: Any, max_bytes: Any) -> None:
    if type(context) is not int or not 0 <= context <= 10:
        raise OrchError("invalid.input", "context must be 0..10")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
        raise OrchError("invalid.input", "max_bytes must be 1024..1048576")


def diff_result(
    caps: Any, store: Any, view: Any, *, repo: str | None = None, context: int = 3, max_bytes: int = 262144
) -> dict[str, Any]:
    """Both phases in one call (direct mode, and the reference the host's split is tested against)."""
    check_args(context, max_bytes)
    return diff_run(caps, diff_plan(store, view, caps, repo=repo), context=context, max_bytes=max_bytes)
