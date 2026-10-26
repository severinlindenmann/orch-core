"""orch's own git reader (D-P2-19, F7, F10): no git process, config read as data, everything hash-checked.

The host never runs git and never opens a repository file itself. It starts a short-lived child,
``python -I -m orch.gitread``, through :func:`orch.host.launcher.spawn_gitread` (the one spawn its audit hook allows).
The child takes its **profile** and **pins** as its first request on stdin, sets its resource limits, installs its own
audit hook and only then opens anything (:mod:`orch.gitread.child`).

Modules (kept import-light: the child loads only what it needs):

* :mod:`~orch.gitread.errors`, :mod:`~orch.gitread.limits`: the refusal and the numeric bounds of section 0.11.
* :mod:`~orch.gitread.fs`: pinned directories, ``O_NOFOLLOW`` opens, regular files only, sizes capped.
* :mod:`~orch.gitread.config`: the strict config parser (three keys, includes ignored), extensions.
* :mod:`~orch.gitread.objects`: loose objects, pack v2 with idx v2, ofs and ref deltas, SHA-1 and SHA-256.
* :mod:`~orch.gitread.refs`: ``HEAD``, loose and packed refs as git resolves them, the F7 refusals.
* :mod:`~orch.gitread.repo`: one opened repository; commits, trees, merge base.
* :mod:`~orch.gitread.myers`, :mod:`~orch.gitread.patchtext`: a clean-room line diff and git-style patch text.
* :mod:`~orch.gitread.pinning`: what a repo's gitdir and ``commondir`` are, once, at registration.
* :mod:`~orch.gitread.ops`: the requests the child answers (``pin``, ``observe``, ``diff``).
* :mod:`~orch.gitread.sandbox`: the child's limits and audit hook (profiles ``gitdir``, ``worktree``, ``pin``).
* :mod:`~orch.gitread.client`: the host side: the pool of at most four children, the pin store, the capability.
* :mod:`~orch.gitread.local`: the same requests in-process (direct mode).
"""
