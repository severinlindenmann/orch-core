from __future__ import annotations


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def render_rules(cfg: dict) -> str:
    git, may, commit = cfg["git"], cfg["git"]["agent_may"], cfg["commit"]
    term = git["review_term"]
    trackers = ", ".join(t["prefix"] for t in cfg["external_trackers"]) or "none"
    skip = ", ".join(cfg["gates"]["plan_skip_sizes"]) or "none"
    body = " / ".join(commit["body"]) + (" / Rollback" if commit["rollback"] else "")
    wiki = cfg["wiki"]["type"] + (f" ({cfg['wiki']['space']})" if cfg["wiki"].get("space") else "")
    lines = [
        f"customer: {cfg['customer']}",
        f"ticket ids: {cfg['id']['prefix']}-{'N' * cfg['id']['pad']} (external: {trackers})",
        f"git: {git['type']} · review term: {term} · branch: {git['branch_pattern']}",
        f"agent may commit: {_yn(may['commit'])} · push: {_yn(may['push'])} · open {term}: {_yn(may['open_review'])}",
        f"commit subject: {commit['subject']} · body: {body}",
        "attribution: forbidden (no Co-Authored-By, no 'Generated with', no tool or model names)"
        if commit["forbid_attribution"] else "attribution: allowed",
        f"gates: requirements → plan (skipped for sizes: {skip}; until it is approved an agent adds tasks but does "
        "not start or finish them) → verify; size and type are part of the requirements approval",
        "requirements gate: binds Summary, Requirements, Acceptance criteria and Out of scope; it refuses while "
        "Requirements or Acceptance criteria are empty: write them into those sections (`orch new "
        "--requirements-file/--acceptance-file`, or `orch section set <id> Requirements --file …`), not into the Ask",
        "tasks: write the list with `orch task add` right after claiming; one task in progress; "
        "testing needs every task done or skipped with a reason (every size)",
        "human-only: approve, request changes, answer, verdict, move to open, backlog, in-progress "
        "or done, reopen, close, `orch epic pause`, `orch ledger adopt`, `orch ledger repair`, moving a ticket into or out of an approved "
        "epic, addon install/update/trust/enable/disable/remove/rollback, `orch widget html on` (agent HTML in widgets, signed; a "
        "config edit alone never turns it on); an "
        "agent is recognised "
        "by its environment and its process ancestry, and never acts as the human",
        "epics: approving an epic covers every child not done; a changed child needs the epic approved again; plans "
        "written after the approval are approved together with `orch approve <epic> plans`; with "
        "the human's delegation an agent approves a child it created with `orch epic auto-approve`, within the limits",
        "ticket file: the Log is append-only and never names the human as actor; questions for the human go "
        "through `orch ask`, never into Requirements or Plan text (such a gate is approved only when the human "
        "overrides it with --despite-open-question)",
        "one review for the human: draft the Plan during refinement (sizes with a plan gate), so the human "
        "approves requirements and plan together in one confirm; ask blocking questions during refinement, not "
        "mid-work; when you create several related tickets, group them under an epic (one charter approval) and "
        "refine each child before handing over",
        "whose move: `orch show <id> --json` and `orch list --json` carry `move` {who, kind, label, why}, the "
        "dashboard's own rule; when `who` is you (the human), wait",
        "waiting on the human: after handing over a gate, asking a blocking question or moving to testing, "
        "run `orch wait <id> --json` in the background and continue when it returns; re-run it on timeout",
        f"wiki: {wiki}",
        f"artifacts: {cfg['artifacts']['mode']} · every file or URL you produce for a ticket goes in with "
        "`orch artifact add` (screenshots, reports, logs, dashboards, PR checks, CI runs): "
        "`orch artifact add <id> shot.png --ac 2 --inline --label \"…\"` · "
        "`orch artifact add <id> --url https://… --kind build --label \"CI run\"`; show, not tell: a screenshot, "
        "a before/after image or a small table beats a paragraph (`![what it shows](artifact:<name>)` in any section)",
    ]
    return "\n".join(lines)
