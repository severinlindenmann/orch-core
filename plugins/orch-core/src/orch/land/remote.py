"""Landing's network half (#596 task 4): fetch the target, push the candidate. The only callers of the two operations
of :data:`orch.gitsafe.LAND_OPS` that have a network.

Neither function decides anything: the remote and branch come from signed settings (F8), the candidate from
:meth:`orch.land.git.LandRepo.candidate`, and *whether a push may happen at all* is task 6's ``authorize_push``
(I3), which must be the only caller of :func:`push_candidate`.
"""

from __future__ import annotations

from orch import gitsafe
from orch.land.git import Candidate, LandGitError, LandRepo, _oid, target_ref
from orch.land.key import KeyStore, LandKeyError

__all__ = ["fetch_target", "push_candidate"]


def _net(store: KeyStore, remote: str) -> gitsafe.NetSpec:
    try:
        return store.net_spec(remote)
    except LandKeyError as e:
        raise LandGitError(e.code, e.reason) from None


def fetch_target(repo: LandRepo, store: KeyStore, remote: str, branch: str) -> str:
    """Fetch only ``refs/heads/<branch>`` of ``remote`` into the host repository's ``refs/land/target/...`` (a local
    name that is a function of the full ref) and return the commit it now holds. Objects are fsck-checked by git."""
    ref = target_ref(branch)
    tref = gitsafe.target_tracking_ref(ref)
    net = _net(store, remote)
    code, _ = repo._run("land_fetch", (remote, ref, tref), ok=tuple(range(256)), net=net)  # noqa: SLF001
    if code != 0:
        raise LandGitError("land.remote_failed", "the remote could not be fetched")
    code, out = repo._run("land_ref", tref, ok=(0, 1))  # noqa: SLF001
    sha = out.decode("ascii", "replace").strip()
    if code != 0 or len(sha) not in (40, 64):
        raise LandGitError("land.remote_failed", "the remote has no such branch")
    return _oid(sha, "the fetched commit")


def push_candidate(repo: LandRepo, store: KeyStore, remote: str, branch: str, candidate: Candidate) -> None:
    """Move ``refs/heads/<branch>`` at ``remote`` from ``candidate.target_sha`` to ``candidate.sha``, and nothing else.

    F2: git's lease lifts the fast-forward rule, so first (1) the candidate must have the recorded shape
    (:meth:`LandRepo.check_push_shape`), (2) the target must be an ancestor of it and (3) it must differ from it. The
    lease then pins the remote's old value exactly (``--force-with-lease=<ref>:<target_sha>``): a remote that moved is
    refused (``land.target_moved``). No ``+``, no ``--force``, one atomic ref. Afterwards the remote is fetched again
    and must show the candidate (``land.push_unverified``).

    **Outcome unknown:** a push that times out (or whose process dies) raises ``land.unavailable``; the remote may or
    may not have taken it. A caller that resumes (task 7) must ``ls-remote``/:func:`fetch_target` before it pushes
    again, never assume "not pushed"."""
    ref = target_ref(branch)
    repo.check_push_shape(candidate)
    target, sha = _oid(candidate.target_sha, "target"), _oid(candidate.sha, "candidate")
    if target == sha or not repo.has(sha) or not repo.is_ancestor(target, sha):
        raise LandGitError("land.changed", "the candidate does not extend the target as recorded")
    net = _net(store, remote)
    code, out = repo._run("land_push", (ref, target, remote, sha, ref), ok=tuple(range(256)), net=net)  # noqa: SLF001
    if code != 0:
        text = out.decode("utf-8", "replace")
        if "stale info" in text or "rejected" in text:
            raise LandGitError("land.target_moved", "the target branch changed on the remote; nothing was pushed")
        raise LandGitError("land.remote_failed", "the push did not go through")
    if fetch_target(repo, store, remote, branch) != sha:
        raise LandGitError("land.push_unverified", "the remote does not show the pushed commit")
