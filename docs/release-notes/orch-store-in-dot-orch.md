# orch keeps its workspace in `.orch/`

**What changes.** New workspaces (`orch init`, `orch setup`, `host.bootstrap`) put everything orch owns in one hidden
folder, `<project>/.orch/`: `config.json`, `keys.jsonl`, `AGENTS.orch.md`, `events/`, `tickets/`, `addons/` and
`.state/`. The project root keeps only the files other tools read there: `AGENTS.md` (with the orch block, now pointing
at `.orch/AGENTS.orch.md`), `CLAUDE.md` (`@AGENTS.md`), `.agents/skills/orch-*`, `.claude/skills/orch-*` and a
`.gitignore` line `.orch/`.

**Why.** A project's own `config.json`, `events/`, `tickets/` or `.state/` no longer collide with orch's files, and a
project with a JavaScript `config.json` can set up orch. `.orch/` is git-ignored as a whole; records travel through
`orch records` (its repository is `.orch/.git`).

**Existing workspaces keep working** (they are read as before) until you move them:

    orch migrate

It stops nothing you did not ask for: it refuses while agents, terminals or sign requests are running, moves the files
with plain renames, checks that every log is byte-identical and replays, and rolls back by itself if anything is off.
Nothing is deleted. A host older than this release is refused with the fix (`orch host install`, restart). If a move is
interrupted, the next host start (or `orch migrate`) finishes or undoes it.

**What to know.**
- `ORCH_WORKSPACE` still names the project folder (`<project>/.orch` is accepted too).
- A project that already has its own `.orch/` folder (another tool) cannot be set up as an orch workspace; rename it.
- Records-enabled workspaces move their records repository with the store only when the host recorded its HEAD.
- Documents: ticket-format §2 and §10.7, host D-P2-5, harnesses.

**After the move period.** Once you have moved your workspaces, a workspace whose store is still in the project folder is
refused by every command with one fix, `orch migrate` (which keeps working); nothing is read or written until it is moved.
