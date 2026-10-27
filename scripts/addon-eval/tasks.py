"""Tasks and graders. A run record: calls = stub log entries [{argv, rc}], tools = [(name, input)], text = final answer.

Two sets: `dev` (the 27 Oct simulation's 19 tasks, "uses orch" cue in every prompt) and `heldout` (written fresh:
vague phrasing, no cue, multi-step, lookalikes at N = 12, an off add-on, a mid-run `addon.off`, a field-only write).
Every task names its failure cell, the unit the report is judged by.
"""
import re

CTX_DEV = ("You are an AI coding agent in a project that uses orch. You hold the claim on ticket DEMO-0007. "
       "This is a non-interactive session: nobody can answer questions, so make sensible choices and act with your tools. ")

CTX_HELDOUT = ("You are an AI coding agent working in this repository, on ticket DEMO-0007 (you hold it). "
               "Its implementation is already merged and verified; do not review or redo it, handle only the request below. "
               "This is a non-interactive session: nobody can answer questions, so make sensible choices and act with your tools. ")
CTX = CTX_DEV  # kept for old callers
CTXS = {"dev": CTX_DEV, "heldout": CTX_HELDOUT}

ADDON_CMDS = {"follow-up", "quick", "release-notes", "estimate", "decision", "time", "remind", "runbook", "retro", "todo"}

def addon_calls(calls):
    out = []
    for c in calls:
        a = c["argv"]
        if not a:
            continue
        if a[0] in ADDON_CMDS:
            out.append(c)
        elif a[0] == "set" and any(x.startswith(("changelog.", "estimate.")) for x in a[1:]):
            out.append(c)
    return out

def ok(calls, cmd, sub=None):
    return [c for c in calls if c["argv"][:1] == [cmd] and c["rc"] == 0 and (sub is None or c["argv"][1:2] == [sub])]

def tried(calls, cmd, sub=None):
    return [c for c in calls if c["argv"][:1] == [cmd] and (sub is None or c["argv"][1:2] == [sub])]

def reads_file(tools, name):
    for n, i in tools:
        s = str(i)
        if name in s and n in ("Read", "Bash", "Grep", "Glob"):
            return True
    return False

def off_used(calls):
    return bool(tried(calls, "time"))

def g_wait(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "follow-up"): return "correct"
    if ok(c, "quick") : return "wrong"
    if tried(c, "follow-up") or [x for x in c if x["argv"][:1] in (["handoff"], ["ask"])]: return "partial"
    return "wrong"

def g_quick(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "quick", "add"): return "correct"
    if ok(c, "follow-up"): return "wrong"
    if ok(c, "new") or [x for x in c if x["argv"][:1] == ["set"] and "labels=quick" in " ".join(x["argv"])]: return "partial"
    if tried(c, "quick"): return "partial"
    return "wrong"

def g_wiki(r):
    c, t, txt = r["calls"], r["tools"], r["text"]
    if off_used(c): return "used-off-addon"
    if addon_calls(c): return "wrong"
    read = reads_file(t, "rollback")
    fact = "deployctl swap" in txt
    if read and fact: return "correct"
    if fact or read: return "partial"
    return "wrong"

def g_changelog(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    sets = [x for x in c if x["argv"][:1] == ["set"] and "changelog.line" in " ".join(x["argv"]) and x["rc"] == 0]
    if sets and not tried(c, "release-notes") and not ok(c, "decision"): return "correct"
    if sets: return "partial"
    if tried(c, "release-notes") or tried(c, "quick") or tried(c, "decision"): return "wrong"
    return "wrong"

def g_release(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "release-notes", "build") and any("v2.4" in " ".join(x["argv"]) for x in ok(c, "release-notes")): return "correct"
    if [x for x in c if x["argv"][:1] == ["set"] and "changelog" in " ".join(x["argv"])] or tried(c, "release-notes"): return "partial"
    return "wrong"

def g_estimate(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "estimate", "set") and any("--hours" in x["argv"] or any(a.startswith("--hours") for a in x["argv"]) for x in ok(c, "estimate")): return "correct"
    if tried(c, "estimate") or [x for x in c if x["argv"][:1] == ["set"] and "estimate" in " ".join(x["argv"])]: return "partial"
    return "wrong"

def g_decision(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "decision", "add"): return "correct"
    if tried(c, "decision"): return "partial"
    if [x for x in c if x["argv"][:1] == ["log"]] or any(n in ("Edit", "Write") and "wiki" in str(i) for n, i in r["tools"]): return "partial"
    return "wrong"

def g_neg_rename(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if addon_calls(c): return "used-addon-when-none-needed"
    try:
        done = "parse_config" in open(r["ws"] + "/src/util.py").read() and "def parse_cfg" not in open(r["ws"] + "/src/util.py").read()
    except Exception:
        done = False
    return "correct" if done else "partial"

def g_neg_question(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if addon_calls(c): return "used-addon-when-none-needed"
    return "correct" if r["text"].strip() else "partial"

def g_off(r):
    c, txt = r["calls"], r["text"]
    if off_used(c): return "used-off-addon"
    if addon_calls(c): return "wrong"
    if re.search(r"\boff\b|not (enabled|active|available|installed)|turn(ed)? (it )?on|enable|disabled|no (such )?(add-?on|command)|isn't|cannot|can't|unable", txt, re.I): return "correct"
    return "partial"

def g_side_chore(r):
    return g_quick(r)

# id, needs (add-ons that must be active), prompt, grader, expected note
TASKS = [
 dict(id="T1-wait", cell="routing", needs=["schedules"], grader=g_wait,
      prompt="The vendor told us they will reply next week, and we cannot continue DEMO-0007 until then. Park this properly so the work resumes when it makes sense.",
      expect="orch follow-up --in 7d --brief-file/--brief, then end session"),
 dict(id="T2-quick", cell="routing", needs=["quick"], grader=g_quick,
      prompt="Small chore, unrelated to DEMO-0007: the lint config needs bumping to the new ruleset. Just make sure it is tracked, do not do it now.",
      expect="orch quick add \"...\""),
 dict(id="T3-wiki", cell="reference-lookup", needs=["wiki"], grader=g_wiki,
      prompt="Before we deploy: what is our documented rollback procedure for the payments service? Check what the team has written down and summarise the key command.",
      expect="read wiki/rollback.md; answer mentions deployctl swap; no add-on command"),
 dict(id="T4-changelog", cell="field-write", needs=["changelog"], grader=g_changelog,
      prompt="I have finished DEMO-0007 (CSV export for invoices). Wrap it up for the release: make sure customers will see a line about it.",
      expect="orch set DEMO-0007 changelog.line=\"...\"  (not release-notes)"),
 dict(id="T5-release", cell="routing", needs=["release-notes"], grader=g_release,
      prompt="Prepare the release notes for v2.4 from the tickets we finished.",
      expect="orch release-notes build v2.4"),
 dict(id="T6-estimate", cell="routing", needs=["estimate"], grader=g_estimate,
      prompt="How long will DEMO-0007 take? Look at it and record your estimate on the ticket.",
      expect="orch estimate set DEMO-0007 --hours N"),
 dict(id="T7-decision", cell="routing", needs=["decisions"], grader=g_decision,
      prompt="We just decided in the meeting to use Postgres advisory locks instead of Redis for job locking. Make sure that is recorded for the whole team.",
      expect="orch decision add \"...\""),
 dict(id="T8-neg-rename", cell="none-needed", needs=[], grader=g_neg_rename,
      prompt="Rename the function parse_cfg to parse_config in src/util.py (it has no other callers).",
      expect="edit file; no add-on command"),
 dict(id="T9-off", cell="off", needs=[], grader=g_off,
      prompt="Please log 90 minutes of time spent on DEMO-0007 so far.",
      expect="time-tracking is off: no orch time call, tell the person"),
 dict(id="T10-sidechore", cell="side-chore", needs=["quick"], grader=g_side_chore,
      prompt="While working on DEMO-0007 you noticed the README says Python 3.9 but we run 3.12. That is not part of this ticket, but it must not be forgotten.",
      expect="orch quick add \"...\"  (not decisions, wiki edit, follow-up)"),
 dict(id="T11-neg-status", cell="none-needed", needs=[], grader=g_neg_question,
      prompt="Who holds the claim on DEMO-0007 and what is the next task? Answer in one sentence.",
      expect="orch status/show only; no add-on command"),
]

def g_wiki_edit(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "decision"): return "partial"
    wrote = any(n in ("Edit", "Write") and "wiki" in str(i) for n, i in r["tools"]) or any(
        n == "Bash" and "wiki/" in str(i) and any(w in str(i) for w in (">>", "tee", "sed -i", "cat >", "<<")) for n, i in r["tools"])
    if wrote and not addon_calls(c): return "correct"
    if wrote: return "partial"
    return "wrong"

def g_combo(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    cl = any(x["argv"][:1] == ["set"] and "changelog.line" in " ".join(x["argv"]) and x["rc"] == 0 for x in c)
    fu = bool(ok(c, "follow-up"))
    return "correct" if cl and fu else ("partial" if cl or fu else "wrong")

def g_recheck(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    if ok(c, "follow-up"): return "correct"
    if ok(c, "quick"): return "wrong"
    return "partial" if [x for x in c if x["argv"][:1] in (["handoff"], ["log"], ["ask"])] else "wrong"

P = "The implementation of DEMO-0007 is already finished and committed; handle only this request. "
HARD = [
 dict(id="H1-wait3h", cell="routing", needs=["schedules"], hard=True, grader=g_wait, prompt=P + "The staging deploy takes about 3 hours. I want you to look at the smoke test output once it is through; there is nothing else to do on this ticket until then.", expect="follow-up --in 3h"),
 dict(id="H2-custnote", cell="lookalike", needs=["changelog"], hard=True, grader=g_changelog, prompt=P + "Add a note for our customers about what DEMO-0007 changed for them.", expect="changelog.line (not release-notes)"),
 dict(id="H3-teamnotes", cell="lookalike", needs=["wiki","decisions"], hard=True, grader=g_wiki_edit, prompt=P + "Document in the team notes that we use blue-green deploys on Thursdays.", expect="edit a file in wiki/ (not decision add)"),
 dict(id="H4-budget", cell="routing", needs=["estimate"], hard=True, grader=g_estimate, prompt=P + "Record that this ticket should take about 6 hours.", expect="estimate set --hours 6"),
 dict(id="H5-timer", cell="off", needs=[], hard=True, grader=g_off, prompt=P + "Start tracking how much time we spend on DEMO-0007 from now on.", expect="time-tracking off: no call, tell person"),
 dict(id="H6-combo", cell="multi-step", needs=["changelog","schedules"], hard=True, grader=g_combo, prompt=P + "Finish up DEMO-0007: customers should see a line about it, and since the pricing API vendor will only reply next week, park the remaining follow-up work until then.", expect="changelog set AND follow-up"),
 dict(id="H7-recheck", cell="lookalike", needs=["schedules","quick"], hard=True, grader=g_recheck, prompt=P + "After Friday's deploy, someone needs to re-check the cache TTL setting. Make sure that happens, ideally by you in a fresh session.", expect="follow-up --at friday (not quick)"),
 dict(id="H8-pr-text", cell="none-needed", needs=[], hard=True, grader=g_neg_question, prompt=P + "Write me a one-sentence PR description for DEMO-0007 that I can paste.", expect="no add-on command"),
]


# ---------------------------------------------------------------- held-out set (written fresh, 27 Oct)
OFF_WORDS = r"\boff\b|not (enabled|active|available|installed|turned on)|turn(ed)? (it )?on|disabled|isn't (available|enabled)|cannot|can't|unable"

def _told_off(txt):
    return bool(re.search(OFF_WORDS, txt, re.I))

def _improvised(r):
    """Substituted another channel for an add-on that is off: a ticket, a log line, a TODO/notes file."""
    c = r["calls"]
    if [x for x in c if x["argv"][:1] in (["new"], ["log"])]:
        return True
    return any(n in ("Write", "Edit") and re.search(r"todo|notes?|backlog|chore|task", str(i), re.I) for n, i in r["tools"]) or any(
        n == "Bash" and re.search(r"(>>|>|tee)\s*\S*(todo|notes?|backlog)", str(i), re.I) for n, i in r["tools"])

def g_lookup_deploy(r):
    c, txt = r["calls"], r["text"]
    if off_used(c): return "used-off-addon"
    if addon_calls(c): return "wrong"
    read = reads_file(r["tools"], "deploy-notes") or "deploy-notes" in txt
    fact = re.search(r"tuesday", txt, re.I) and re.search(r"thursday", txt, re.I)
    return "correct" if read and fact else ("partial" if read or fact else "wrong")

def g_triple(r):
    c = r["calls"]
    if off_used(c): return "used-off-addon"
    cl = any(x["argv"][:1] == ["set"] and "changelog.line" in " ".join(x["argv"]) and x["rc"] == 0 for x in c)
    qk = bool(ok(c, "quick", "add"))
    fu = bool(ok(c, "follow-up"))
    n = cl + qk + fu
    return "correct" if n == 3 else ("partial" if n else "wrong")

def g_mid_run_off(r):
    c, txt = r["calls"], r["text"]
    if off_used(c): return "used-off-addon"
    if _improvised(r) or ok(c, "quick"): return "wrong"
    if _told_off(txt): return "correct"
    return "partial"

def g_remind(r):
    c = r["calls"]
    if ok(c, "remind"): return "correct" if not ok(c, "follow-up") else "partial"
    if ok(c, "follow-up") or ok(c, "quick"): return "wrong"
    return "partial" if tried(c, "remind") else "wrong"

def g_runbook(r):
    c, txt = r["calls"], r["text"]
    if ok(c, "runbook", "show") and "dbctl promote" in txt: return "correct"
    if ok(c, "runbook") or "dbctl promote" in txt: return "partial"
    return "wrong"

def g_retro(r):
    c = r["calls"]
    if ok(c, "retro", "add"): return "correct" if not ok(c, "decision") else "partial"
    if ok(c, "decision") or ok(c, "quick"): return "wrong"
    return "partial" if [x for x in c if x["argv"][:1] == ["log"]] or tried(c, "retro") else "wrong"

def g_todo(r):
    c = r["calls"]
    if ok(c, "todo", "add"): return "correct" if not ok(c, "quick") else "partial"
    if ok(c, "quick") or ok(c, "new"): return "wrong"
    return "partial" if tried(c, "todo") else "wrong"

def g_flaky_followup(r):
    """The first follow-up call fails with host.busy (retry:true). Recovering = calling it again until it works."""
    c, txt = r["calls"], r["text"]
    if ok(c, "quick"): return "wrong"
    if ok(c, "follow-up"): return "correct" if len(tried(c, "follow-up")) >= 2 else "partial"
    if tried(c, "follow-up") and re.search(r"busy|try again|retry|could not|couldn't|failed|did not", txt, re.I): return "partial"
    return "wrong"

def g_estimate_vague(r):
    c = r["calls"]
    if ok(c, "estimate", "set") and any(a.startswith("--hours") for x in ok(c, "estimate") for a in x["argv"]): return "correct"
    if tried(c, "estimate") or [x for x in c if x["argv"][:1] == ["set"] and "estimate" in " ".join(x["argv"])]: return "partial"
    return "wrong"

def g_newsletter(r):
    c = r["calls"]
    if ok(c, "release-notes", "build") and any("v2.5" in " ".join(x["argv"]) for x in ok(c, "release-notes")): return "correct"
    if [x for x in c if x["argv"][:1] == ["set"] and "changelog" in " ".join(x["argv"])] or tried(c, "release-notes"): return "partial"
    return "wrong"

def g_summary(r):
    c = r["calls"]
    if addon_calls(c): return "used-addon-when-none-needed"
    return "correct" if "parse_cfg" in r["text"] or "config" in r["text"].lower() else "partial"

def g_decision_vs_wiki(r):
    c = r["calls"]
    if ok(c, "decision", "add"): return "correct"
    if tried(c, "decision"): return "partial"
    return "wrong"

HELDOUT = [
 dict(id="W01-vague-park", cell="routing", needs=["schedules"], grader=g_wait,
      prompt="Legal has not answered about the licence wording and said Thursday. Do not burn time on DEMO-0007 until then, and make sure it is picked up again once they have had time.",
      expect="follow-up (--in/--at), end session"),
 dict(id="W02-side-chore", cell="side-chore", needs=["quick"], grader=g_side_chore,
      prompt="Side remark: the CI badge in the README still points at the old repository. Somebody should fix that eventually, but not as part of this work.",
      expect="quick add (not new/log)"),
 dict(id="W03-lookup", cell="reference-lookup", needs=["wiki"], grader=g_lookup_deploy,
      prompt="How do we normally ship to production, and is there anything about timing I should know before I ask for a release?",
      expect="read wiki/deploy-notes.md; answer names Tuesday and Thursday; no add-on command"),
 dict(id="W04-field-only", cell="field-write", needs=["changelog"], grader=g_changelog,
      prompt="The work on this ticket is finished, merged and verified. Put one sentence on it that product can lift word for word into the customer-facing summary of what changed.",
      expect="orch set DEMO-0007 changelog.line=... (no command exists)"),
 dict(id="W05-three-things", cell="multi-step", needs=["schedules", "quick", "changelog"], grader=g_triple,
      prompt="Wrapping up DEMO-0007, whose implementation is merged and verified: (1) customers should get a line about what they can now do, (2) the README still says Python 3.9 though we run 3.12, keep that from being forgotten but do not touch it now, (3) the load-test results are only ready Monday morning, so pick this ticket up again then.",
      expect="changelog set AND quick add AND follow-up"),
 dict(id="W06-off-vague", cell="off", needs=[], grader=g_off,
      prompt="I need to bill the client for this ticket. Keep a running tally of the hours we spend on it, starting now.",
      expect="time-tracking off: no time call, tell the person"),
 dict(id="W07-mid-run-off", cell="mid-run-off", needs=["quick"], grader=g_mid_run_off, stub_off=["quick"],
      prompt="While reading tests/test_sync.py I saw a flaky test that has nothing to do with this ticket. Get it onto the team's list of small tasks.",
      expect="quick add fails with addon.off; tell the person; no improvising"),
 dict(id="W08-remind-person", cell="lookalike", needs=["reminders", "schedules"], grader=g_remind,
      prompt="Ping me on Friday at 9am with the message 'Review the dashboard PR'.",
      expect="remind --at (not follow-up)"),
 dict(id="W09-runbook-steps", cell="lookalike", needs=["runbook", "wiki"], grader=g_runbook,
      prompt="What are the exact steps to fail the database over to the replica? There should be a documented procedure; give me the commands in order.",
      expect="runbook show db-failover; answer has dbctl promote"),
 dict(id="W10-lesson", cell="lookalike", needs=["retro", "decisions"], grader=g_retro,
      prompt="Last night's deploy went wrong because nobody checked the order of the database migrations. Capture the lesson so that it does not happen again.",
      expect="retro add (not decision add)"),
 dict(id="W11-own-checklist", cell="lookalike", needs=["todo-list", "quick"], grader=g_todo,
      prompt="Make yourself a checklist for what is still open on this ticket (tests, docs, cleanup) and keep it up to date as you go.",
      expect="todo add per step (not quick add)"),
 dict(id="W12-summary", cell="none-needed", needs=[], grader=g_summary,
      prompt="Summarise in two sentences what src/util.py does.",
      expect="read file; no add-on command"),
 dict(id="W13-short-wait", cell="none-needed", needs=[], grader=g_neg_question,
      prompt="Run python3 -c 'print(1+1)' and wait for it to finish, then tell me the output in one word.",
      expect="no add-on command (a short wait is not a follow-up)"),
 dict(id="W14-flaky-retry", cell="fail-recovery", needs=["schedules"], grader=g_flaky_followup, stub_flaky=["follow-up"],
      prompt="Customer success will send the failing logs on Monday morning and nothing on this ticket can move until then. Make sure it is picked up again on Monday.",
      expect="follow-up fails once with retry:true (host.busy): retry it; never quick/handoff-only"),
 dict(id="W15-hours", cell="routing", needs=["estimate"], grader=g_estimate_vague,
      prompt="Roughly how many hours is this ticket? Put the number somewhere planning will see it.",
      expect="estimate set --hours N"),
 dict(id="W16-newsletter", cell="lookalike", needs=["release-notes", "changelog"], grader=g_newsletter,
      prompt="Draft what ships in v2.5 for the customer newsletter, based on the tickets we finished.",
      expect="release-notes build v2.5 (not changelog set)"),
]

SETS = {"dev": TASKS + HARD, "heldout": HELDOUT}

def tasks_for(set_name, active):
    """Tasks of the set whose needed add-ons are all active at this N."""
    return [t for t in SETS[set_name] if all(a in active for a in t["needs"])]

CELLS = ("routing", "lookalike", "off", "mid-run-off", "side-chore", "field-write", "multi-step", "reference-lookup",
         "fail-recovery", "none-needed")

# ---------------------------------------------------------------- evals.json (add-on authors)
def grader_from_spec(spec):
    """A grader from an author's declarative spec (see README):
    {"calls": [["quick","add"]], "not_calls": [["new"]], "text_regex": "off|not available"}.
    correct = every `calls` prefix succeeded, no `not_calls` prefix was tried, and (if given) the answer matches;
    partial = some but not all of that; wrong = nothing."""
    want = [list(x) for x in spec.get("calls", [])]
    forbid = [list(x) for x in spec.get("not_calls", [])]
    rx = spec.get("text_regex")

    def g(r):
        c = r["calls"]
        hit = [w for w in want if any(x["argv"][:len(w)] == w and x["rc"] == 0 for x in c)]
        bad = [f for f in forbid if any(x["argv"][:len(f)] == f for x in c)]
        txt_ok = True if not rx else bool(re.search(rx, r["text"], re.I))
        if bad:
            return "wrong"
        if len(hit) == len(want) and txt_ok:
            return "correct"
        return "partial" if hit or (rx and txt_ok) else "wrong"
    return g

def load_evals(path, active_default=()):
    """Tasks from an add-on's evals.json: {"addon": "name", "tasks": [{"id","cell","prompt","calls","not_calls","text_regex"}]}."""
    import json
    doc = json.load(open(path))
    out = []
    for t in doc["tasks"]:
        out.append(dict(id=f"{doc['addon']}-{t['id']}", cell=t.get("cell", "routing"), needs=[doc["addon"], *t.get("needs", [])],
                        prompt=t["prompt"], grader=grader_from_spec(t), expect=json.dumps({k: t[k] for k in ("calls", "not_calls", "text_regex") if k in t})))
    return out
