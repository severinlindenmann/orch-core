# orch-core (v2)

The orch v2 core (see `docs/architecture/orch-v2-core.md`).

`orch import v1 PATH` is the migration from v1: it is the only supported way to bring a v1 workspace into v2.

## Migration from v1

A v1 workspace (an `orchestrator/` folder with a `tickets/` directory and `config.json` of `"schema": 1` with an `id` object, the importer's layout) is never opened by a v2 command. Walking up from the working directory, the first folder that is a v2 or a v1 workspace wins; if it is a v1 one, the command refuses with `<path> is a v1 workspace; run `orch import v1` to migrate it` (also when `ORCH_WORKSPACE` names it), even when a v2 workspace lies further up. The starting path (working directory or `ORCH_WORKSPACE`) is resolved and trusted; inside it, `orchestrator/`, `config.json` and `tickets/` are opened only through `O_NOFOLLOW` directory fds (`orch.discovery`), so a symlink there is never followed, and only `config.json` is read. Walking up without `ORCH_WORKSPACE` has an ownership boundary: a folder counts only if it (or its `orchestrator/`) and its `config.json` are yours and not group- or other-writable, and the walk stops at the first sticky or other-writable folder (`/tmp`, `/Users/Shared`), so a workspace someone else planted in or above a shared folder is never used and never blocks `orch setup`. The stop includes the starting folder: in `/tmp` itself nothing is found, and a workspace directly in a sticky folder needs `ORCH_WORKSPACE` or a working directory inside it. A folder the walk cannot open or inspect also stops it (fail closed). The walk goes up through `..` from the open folder, never by path. `ORCH_WORKSPACE` is your explicit choice and skips the ownership check. `orch import v1 PATH` is the only migration path: it reads the v1 folder and writes a new v2 workspace.

## Removing a worktree

`orch worktree remove TICKET` (and the host's `worktree.remove`) never depends on ignore rules: every file in the
worktree that is not tracked and unchanged blocks the removal, **ignored files too** (`node_modules`, `.venv`, build
output). The refusal lists the files, and for an untracked directory its file count and size. Commit what you want to
keep, then either delete the rest yourself or run `orch worktree remove TICKET --discard-untracked=TOKEN` (the refusal prints the
list and its token; a different set means a refusal with the new list and token): it prints exactly
the untracked set it will delete, checks the worktree again right before deleting, and refuses if anything changed in
between. Modified tracked files still block it. `--force` (a person only) removes the worktree whatever it holds.
