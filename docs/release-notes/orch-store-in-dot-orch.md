# orch keeps its workspace in `.orch/`

**What changes.** New workspaces (`orch setup`, `host.bootstrap`) put everything orch owns in one hidden
folder, `<project>/.orch/`: `config.json`, `keys.jsonl`, `AGENTS.orch.md`, `events/`, `tickets/`, `addons/` and
`.state/`. The project root keeps only the files other tools read there: `AGENTS.md` (with the orch block, now pointing
at `.orch/AGENTS.orch.md`), `CLAUDE.md` (`@AGENTS.md`), `.agents/skills/orch-*`, `.claude/skills/orch-*` and a
`.gitignore` line `.orch/`.

**Why.** A project's own `config.json`, `events/`, `tickets/` or `.state/` no longer collide with orch's files, and a
project with a JavaScript `config.json` can set up orch. `.orch/` is git-ignored as a whole; records travel through
`orch records` (its repository is `.orch/.git`).

**Start fresh.** There is no move from the old layout. A folder that still has an orch store from an older version in
the project root (`config.json`, `keys.jsonl`, `events/`, `tickets/`, `.state/`, `addons/`) is refused by every command
with one message that lists those files: delete them and run `orch setup` again.

**What to know.**
- `ORCH_WORKSPACE` still names the project folder (`<project>/.orch` is accepted too).
- A project that already has its own `.orch/` folder (another tool) cannot be set up as an orch workspace; rename it.
- Records-enabled workspaces move their records repository with the store only when the host recorded its HEAD.
- Documents: ticket-format §2 and §10.7, host D-P2-5, harnesses.
