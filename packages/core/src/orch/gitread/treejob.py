"""The ``tree.check`` job (section 0.8.2, H10/H10a): its input, its result, and the tier-A runner.

The job kind is the one the phase-3 worker will take over unchanged:

* :func:`job_input` builds ``TreeCheckInput = {repo, gitdir:Pin, commondir:Pin, worktree:Pin}`` from a repository's
  pins (taken at registration, :class:`orch.gitread.client.PinStore`);
* :func:`run_child` runs it: one ``python -I -m orch.gitread`` child under the ``worktree`` profile (fixed argv,
  empty environment, the section 0.11 limits, the child's audit hook), answering the raw check of
  :mod:`orch.gitread.treecheck`;
* :func:`result_of` checks that raw answer for shape and turns it into
  ``TreeCheckResult = {changed:[{path, why}]{0..200}, more, head, head_ref}``, every path escaped with F1 section
  11.3's display rules (:func:`escape_path`), so nothing the agent named reaches a person unescaped. ``head`` (the
  commit, ``None`` on an unborn branch) and ``head_ref`` (``refs/heads/<branch>``) are what the clean answer is
  relative to, read in the same child as the tree: callers require them to equal the signed ``source_sha`` entry, so
  "clean" is always clean **at the commit being signed** (review of #413). They are for the host, not for display.

**Tier A (phase 1)**: there is no worker UID, so the host itself runs :func:`run_child` through its
:class:`~orch.gitread.client.ReaderPool` (the child still runs the ``worktree`` profile, as the host UID; the host
never opens a repository file). **Phase 3**: the worker gets the same input over ``job.next``, runs the same child
under the worker UID and answers ``job.done`` with the same ``TreeCheckResult``; only :func:`run_child`'s caller
changes. Until then a tier-B workspace has no clean-tree check and ``verdict`` and ``approve code`` fail closed.

A check that cannot establish a clean answer raises :class:`~orch.gitread.errors.Unavailable` ("unknown"); callers
fail closed (``observe.unavailable``).
"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from orch import canon

from .errors import Unavailable
from .fs import RepoPins

__all__ = ["KIND", "WHY", "check_entry", "escape_path", "job_input", "pins_of", "render", "result_of", "run_child"]

KIND = "tree.check"
WHY = ("modified", "untracked", "deleted", "type", "flag")
MAX_CHANGED = 200
PATH_MAX = 4096


def job_input(repo: str, pins: RepoPins) -> dict[str, Any]:
    """``TreeCheckInput`` for the declared repository ``repo`` (its ``settings.repos`` name)."""
    if not isinstance(repo, str) or not 1 <= len(repo) <= 64:
        raise Unavailable("bad tree.check input: repo")
    return {
        "repo": repo,
        "gitdir": pins.gitdir.to_json(),
        "commondir": pins.commondir.to_json(),
        "worktree": pins.worktree.to_json(),
    }


def pins_of(job: dict[str, Any]) -> RepoPins:
    """The pins a ``TreeCheckInput`` names (refused unless well formed)."""
    if not isinstance(job, dict) or set(job) != {"repo", "gitdir", "commondir", "worktree"}:
        raise Unavailable("bad tree.check input")
    for k in ("gitdir", "commondir", "worktree"):
        p = job[k]
        if not isinstance(p, dict) or not isinstance(p.get("path"), str) or not 1 <= len(p["path"]) <= 1024:
            raise Unavailable(f"bad tree.check input: {k}")
    return RepoPins.from_json({k: job[k] for k in ("worktree", "gitdir", "commondir")})


def run_child(pool: Any, principal: str, job: dict[str, Any], *, removal: bool = False) -> dict[str, Any]:
    """Run one ``tree.check`` in a reader child (``worktree`` profile) and return the checked ``TreeCheckResult``.
    ``removal``: the check a removal needs (no ignore rule, untracked directories whole with sizes; #583 round 3)."""
    hello = {"profile": "worktree", "pins": pins_of(job).to_json()}
    args = {"removal": True} if removal else {}
    return result_of(pool.call(principal, hello, {"op": "tree_check", "args": args}))


def escape_path(raw: bytes, cut: bool = False) -> str:
    """A path from the agent's worktree as display text (F1 section 11.3, N3): valid UTF-8 runs go through
    :func:`orch.canon.clean` (controls, bidi and other invisible characters become ``⟨U+XXXX⟩``; a literal ``⟨`` is
    itself escaped, so no path can forge a marker), a line feed becomes ``⟨U+000A⟩``, a byte that is not UTF-8 becomes
    ``⟨0xHH⟩``. ``cut``: the path was longer than :data:`PATH_MAX` bytes and ends in ``…``."""
    out: list[str] = []
    text = raw.decode("utf-8", errors="surrogateescape")
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append(canon.clean("".join(run)).replace("\n", "⟨U+000A⟩"))
            run.clear()

    for ch in text:
        o = ord(ch)
        if 0xDC80 <= o <= 0xDCFF:
            flush()
            out.append(f"⟨0x{o - 0xDC00:02X}⟩")
        else:
            run.append(ch)
    flush()
    s = "".join(out)
    if len(s) > PATH_MAX - 1 or cut:
        s = s[: PATH_MAX - 1] + "…"
    return s


def result_of(raw: Any) -> dict[str, Any]:
    """The child's raw answer, checked, as ``TreeCheckResult`` with escaped paths. Anything malformed is
    :class:`Unavailable` (fail closed), never a partial result."""
    if not isinstance(raw, dict) or not isinstance(raw.get("changed"), list) or type(raw.get("more")) is not bool:
        raise Unavailable("the tree check answered nonsense")
    from .refs import check_refname

    head = raw.get("head")
    if head is not None and (
        not isinstance(head, str) or len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head)
    ):
        raise Unavailable("the tree check answered nonsense")
    head_ref = raw.get("head_ref")
    if not isinstance(head_ref, str) or not head_ref.startswith("refs/heads/") or not check_refname(head_ref):
        raise Unavailable("the tree check answered a malformed HEAD")
    if raw["more"] and not raw["changed"]:
        raise Unavailable("the tree check answered nonsense")  # "more" than nothing: never read as clean
    if len(raw["changed"]) > MAX_CHANGED:
        raise Unavailable("the tree check answered too many paths")
    changed = []
    for c in raw["changed"]:
        if not isinstance(c, dict) or set(c) != {"path_b64", "cut", "why"} or c["why"] not in WHY:
            raise Unavailable("the tree check answered a malformed entry")
        if type(c["cut"]) is not bool or not isinstance(c["path_b64"], str):
            raise Unavailable("the tree check answered a malformed entry")
        try:
            path = base64.b64decode(c["path_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise Unavailable("the tree check answered a malformed path") from None
        if not path or len(path) > PATH_MAX:
            raise Unavailable("the tree check answered a malformed path")
        changed.append({"path": escape_path(path, c["cut"]), "why": c["why"]})
    out = {"changed": changed, "more": raw["more"], "head": head, "head_ref": head_ref}
    if "sizes" in raw or "fingerprint" in raw:  # a removal check (#583 round 3): sizes of whole directories
        sizes, fp = raw.get("sizes"), raw.get("fingerprint")
        if not isinstance(sizes, list) or len(sizes) > MAX_CHANGED:
            raise Unavailable("the tree check answered malformed sizes")
        if not isinstance(fp, str) or len(fp) != 64 or any(ch not in "0123456789abcdef" for ch in fp):
            raise Unavailable("the tree check answered a malformed fingerprint")
        by_path: dict[str, tuple[int, int]] = {}
        for e in sizes:
            if not isinstance(e, dict) or set(e) != {"path_b64", "bytes", "files"}:
                raise Unavailable("the tree check answered malformed sizes")
            if type(e["bytes"]) is not int or type(e["files"]) is not int or e["bytes"] < 0 or e["files"] < 0:
                raise Unavailable("the tree check answered malformed sizes")
            try:
                raw_path = base64.b64decode(e["path_b64"], validate=True)
            except (binascii.Error, ValueError, TypeError):
                raise Unavailable("the tree check answered a malformed path") from None
            by_path[escape_path(raw_path[:PATH_MAX])] = (e["bytes"], e["files"])
        for c in changed:
            if c["path"] in by_path:
                c["bytes"], c["files"] = by_path[c["path"]]
        out["fingerprint"] = fp
    rules = raw.get("ignore_rules")
    if isinstance(rules, str):
        out["ignore_rules"] = canon.clean(rules)[:300]
    return out


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


def render(result: dict[str, Any], limit: int = 20) -> str:
    """``TreeCheckResult`` as lines (``why: path``, with ``(N files, size)`` for an untracked directory a removal
    check reported whole); empty when clean. Paths are already escaped."""
    lines = [
        f"{c['why']}: {c['path']}" + (f" ({c['files']} files, {_size(c['bytes'])})" if "files" in c else "")
        for c in result["changed"][:limit]
    ]
    if result["more"] or len(result["changed"]) > limit:
        lines.append("and more")
    return "\n".join(lines)


def check_entry(repo: str, at: float) -> dict[str, Any]:
    """One entry of a prepared request's ``checks`` (N3): not signed, not in ``material``; the signer shows it as
    attested by the worker, best effort. In tier A the host's own reader child plays the worker's part."""
    return {
        "repo": repo,
        "clean": True,
        "attested_by": "worker",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(at))),
    }
