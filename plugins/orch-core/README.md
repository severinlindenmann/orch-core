# orch-core (v2, in development)

The orch v2 core, rebuilt fresh on `develop` (see `docs/architecture/orch-v2-core.md`). orch v1 lives on `main`.

## Migration from v1

A v1 workspace (a folder with `config.json` of `"schema": 1`, usually `orchestrator/`) is never opened by a v2 command. Run in or under such a folder, a v2 command refuses with `<path> is a v1 workspace; run `orch import v1` to migrate it`. `orch import v1 PATH` is the only migration path: it reads the v1 folder and writes a new v2 workspace.
