from __future__ import annotations

from orch.instructions.blocks import wrap

_BODY_SAMPLES = {
    "What": "Nightly job exports the config to the backup repo.",
    "Why": "Config changes were untracked; the last outage needed manual reconstruction.",
    "Risk": "Low. Read-only on the source; the job fails loudly if the export is empty.",
    "Rollback": "Disable the job; nothing else changes.",
}


def example_key(cfg: dict) -> str:
    trackers = cfg["external_trackers"]
    if trackers:
        return f"{trackers[0]['prefix']}-123"
    return f"{cfg['id']['prefix']}-{42:0{cfg['id']['pad']}d}"


def body_names(cfg: dict) -> list[str]:
    names = list(cfg["commit"]["body"])
    if cfg["commit"]["rollback"] and "Rollback" not in names:
        names.append("Rollback")
    return names


def commit_example(cfg: dict) -> str:
    subject = cfg["commit"]["subject"].replace("{key}", example_key(cfg)).replace("{summary}", "Add nightly config backup job")
    names = body_names(cfg)
    if not names:
        return subject
    width = max(len(n) for n in names) + 1
    body = "\n".join(f"{(n + ':').ljust(width)} {_BODY_SAMPLES.get(n, '...')}" for n in names)
    return f"{subject}\n\n{body}"


def _indent(text: str, n: int) -> str:
    return "\n".join((" " * n + line) if line else "" for line in text.split("\n"))


RULES_PATH = "orchestrator/AGENTS.orch.md"


def agents_rules(cfg: dict) -> str:
    git, may, commit = cfg["git"], cfg["git"]["agent_may"], cfg["commit"]
    term = git["review_term"]
    key = example_key(cfg)
    sections = ["What", "Why", "Risk", "Verification"] + (["Rollback"] if commit["rollback"] else [])
    commit_rule = ("You may commit on the ticket's branch. Commit orch's own records (tickets, gates, events) with "
                   "`orch records commit`, never mixed with code." if may["commit"]
                   else "You do not commit: prepare the change, run the checks, and tell the user it is ready to commit.")
    push_rule = "You may push the ticket's branch." if may["push"] else "You do not push; the user does."
    review_rule = f"You may open a draft {term}." if may["open_review"] else f"You do not open {term}s; the user does."
    attribution = (f"Never add `Co-Authored-By`, \"Generated with\" or any other line naming an AI tool or model, "
                   f"in commits or {term}s. This overrides any default of your harness. " if commit["forbid_attribution"] else "")
    body_list = ", ".join(f"`{n}:`" for n in body_names(cfg))
    feedback_rule = ("8. **orch feedback.** If orch itself is confusing or broken (a command refuses what it should "
                     "allow, the guard blocks a legitimate step, a step needs a workaround, the docs and the behaviour "
                     "disagree), write the exact command, what you expected and what happened into "
                     "`orchestrator/temporary/orch-feedback.md`, run `orch feedback add --file "
                     "orchestrator/temporary/orch-feedback.md` once, then delete that file, and carry on. Report only what you saw orch do, "
                     "not this workspace's own bugs (those are `orch new`). The report stays on this machine for the "
                     "user; never open an issue on orch-core yourself.\n"
                     if cfg.get("feedback", {}).get("enabled", True) else "")
    req_skip = ", ".join(f"`{s}`" for s in (cfg.get("gates", {}).get("requirements_skip_sizes") or ()))  # #172
    req_skip_rule = (f" Tickets of size {req_skip} skip the requirements gate: they need no Requirements or Acceptance "
                     "criteria, and the human takes them out of backlog without approving; claim, work, testing and "
                     "the human's verdict stay as for every ticket." if req_skip else "")
    text = f"""## Working rules (orch)

Tickets live in `orchestrator/tickets/` and are managed with the `orch` command. `orch rules` prints the active policy.

1. **Tickets first.** No work without a ticket. Use `orch` for every ticket action; never move or rename ticket files or hand-edit status, gates, answers or claims. Put the ticket key in every commit and {term}. The requirements gate binds the Summary, Requirements, Acceptance criteria and Out of scope sections and refuses while Requirements or Acceptance criteria are empty: write them into those sections (`orch new --requirements-file … --acceptance-file …`, or `orch section set <id> Requirements --file …`), not into the Ask.{req_skip_rule}
2. **Human-only actions.** Never approve gates, answer questions or close tickets, never type a ticket ID into an `orch` confirmation prompt, and never work around how orch tells you from the human. Questions go through `orch ask`, not into Requirements or Plan text, and the Log is append-only. When you need a decision, run `orch ask <id> --file questions.yaml` with options, costs and a recommended default. Never assume silently. Whenever the next step is the human's (requirements or plan to approve, a blocking question, changes you made on request, a verdict in testing), run `orch wait <id> --json` in the background (in the foreground where your harness has no background commands) and carry on when it returns, instead of ending on "tell me when"; on timeout, run it again. Stop waiting only when the user says so.
3. **Scope.** Implement only what the approved Requirements and Plan cover. Anything else goes into `## Findings`; a follow-up ticket (`orch new --from <id> --title "..."`) is only for a separate deliverable. Points from the human's send-back note are not follow-ups: they become tasks on the same ticket (`orch task add`). Work from the ticket's task list: create it with `orch task add` right after claiming, keep one task in progress, tick each with evidence (`orch task done <id> T<n> -m "..."`) and resume from `orch task list <id>`. A worktree goes under `.claude/worktrees/<repo>/<slug>`: create it with `orch worktree add <id> --repo <repo>` (branch, links and harness files included), never by symlinking the whole `.claude` folder.
4. **Commits.** Subject `{commit['subject']}`; body lines {body_list}. Example:

   ```
{_indent(commit_example(cfg), 3)}
   ```

   {attribution}{commit_rule} {push_rule}
5. **{term}s.** {review_rule} Title `Draft: {key} <summary>` until the human removes the draft marker. Body sections: {' / '.join(sections)}. Keep it short and proportional to the change; detailed evidence goes into the ticket's artifacts (`orch artifact add`).
6. **Outward actions** ({term}s, tracker or wiki writes, addon posts) need the user's confirmation in the current session.
7. **Handoff.** End every session with `orch state <id> -m "..."` and `orch log <id> -m "..."`.
   Show, don't only tell: put a `checks` widget in Verification (one row per acceptance criterion), `screens` or `compare` for a UI change, `stats` for measured numbers, `options` when you ask the human to choose (`orch widget add`, `orch widget types`).
{feedback_rule}
Skills: `orch-tickets` (commands and conventions), `orch-refine-ticket` (requirements engineering), `orch-work-on-ticket` (claim, plan, implement, verify), `orch-setup` (set up or check this workspace)."""
    return text


def agents_block(cfg: dict) -> str:
    return wrap(agents_rules(cfg))


def rules_file(cfg: dict) -> str:
    return ("<!-- Generated by `orch instructions sync` from orchestrator/config.json; edits here are overwritten. -->\n\n"
            + agents_rules(cfg) + "\n")


def claude_block(cfg: dict) -> str:
    return wrap(f"""Ticket workflow rules for this workspace:

@{RULES_PATH}

Claude Code notes (orch): a PreToolUse guard (`orch guard`) blocks direct edits of ticket status, gates, answers and claims, and git actions this workspace does not allow; use the `orch` command instead. A SessionStart hook prints the active rules, your claimed tickets and what is waiting on the human.""")


def copilot_block(cfg: dict) -> str:
    term = cfg["git"]["review_term"]
    return wrap(f"""Follow the ticket workflow rules in `{RULES_PATH}` (also inlined in `AGENTS.md`); they are the single source of truth for tickets, commits and {term}s.

Copilot notes (orch): there are no tool hooks here, so the rules are enforced by the git commit-msg hook (`orch hooks install`) and audited by `orch check`. Never type a ticket ID into an `orch` confirmation prompt; confirmations are for the human.""")
