# orch-core (v2)

The orch v2 core (see `docs/architecture/orch-v2-core.md`).

`orch import v1 PATH` is the migration from v1: it is the only supported way to bring a v1 workspace into v2.

## Migration from v1

A v1 workspace (an `orchestrator/` folder with a `tickets/` directory and `config.json` of `"schema": 1` with an `id` object, the importer's layout) is never opened by a v2 command. Walking up from the working directory, the first folder that is a v2 or a v1 workspace wins; if it is a v1 one, the command refuses with `<path> is a v1 workspace; run `orch import v1` to migrate it` (also when `ORCH_WORKSPACE` names it), even when a v2 workspace lies further up. The starting path (working directory or `ORCH_WORKSPACE`) is resolved and trusted; inside it, `orchestrator/`, `config.json` and `tickets/` are opened only through `O_NOFOLLOW` directory fds (`orch.discovery`), so a symlink there is never followed, and only `config.json` is read. `orch import v1 PATH` is the only migration path: it reads the v1 folder and writes a new v2 workspace.
