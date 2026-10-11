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

The op itself (registry entry, error catalog line for ``repo.remote_mismatch``, visibility) is served by the
dispatcher group; this module is the part H3b1 owns.
"""

from __future__ import annotations

import base64
from typing import Any

from orch.ops.errors import OrchError

__all__ = ["diff_result"]


def diff_result(
    caps: Any, store: Any, view: Any, *, repo: str | None = None, context: int = 3, max_bytes: int = 262144
) -> dict[str, Any]:
    from orch.gitread.errors import Busy, RemoteMismatch, Unavailable
    from orch.ops.caps import CapabilityDenied

    if type(context) is not int or not 0 <= context <= 10:
        raise OrchError("invalid.input", "context must be 0..10")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
        raise OrchError("invalid.input", "max_bytes must be 1024..1048576")
    links = view.fields["links"]
    repos = store.state.workspace.repos
    names = sorted(links["repos"])
    if repo is not None:
        if repo not in names:
            raise OrchError("not_found", f"{repo} is not linked to this ticket")
        names = [repo]
    sources: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    truncated = False
    for name in names:
        branch = links["branches"].get(name)
        path = caps.git.repo_path(store.root, repos, name)
        if not branch:
            raise OrchError("observe.unavailable", f"{name}: the ticket names no branch")
        if path is None:
            raise OrchError("observe.unavailable", f"{name}: no working copy in settings.repos")
        spec = repos[name]
        try:
            got = caps.git.diff(
                path,
                name=name,
                branch=branch,
                default_branch=spec.get("default_branch"),
                remote=spec.get("remote"),
                context=context,
                max_bytes=max_bytes,
            )
        except Busy as e:
            raise OrchError("quota.host", f"{name}: {e.reason}", retryable=True) from None
        except RemoteMismatch as e:
            raise OrchError("repo.remote_mismatch", f"{name}: {e.reason}") from None
        except (Unavailable, CapabilityDenied) as e:
            raise OrchError("observe.unavailable", f"{name}: {getattr(e, 'reason', str(e))}"[:200]) from None
        sources.append(
            {
                "repo": name,
                "branch": branch,
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
