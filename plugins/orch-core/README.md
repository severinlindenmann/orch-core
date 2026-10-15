# orch-core (v2, in development)

The orch v2 core, rebuilt fresh on `develop` (see `docs/architecture/orch-v2-core.md`). orch v1 lives on `main`.

## Migration from v1

A v1 workspace (an `orchestrator/` folder with a `tickets/` directory and `config.json` of `"schema": 1` with an `id` object, the importer's layout) is never opened by a v2 command. Walking up from the working directory, the first folder that is a v2 or a v1 workspace wins; if it is a v1 one, the command refuses with `<path> is a v1 workspace; run `orch import v1` to migrate it` (also when `ORCH_WORKSPACE` names it), even when a v2 workspace lies further up. Detection never follows a symlink in any component of the path and reads only `config.json`. `orch import v1 PATH` is the only migration path: it reads the v1 folder and writes a new v2 workspace.
