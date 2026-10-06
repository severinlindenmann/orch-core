from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass

from orch.config.load import validate_schema
from orch.core import evidence, ledger, store
from orch.core.events import Actor, append_event, read_events, scan_events
from orch.core.gates import GATE_SECTIONS, gate_hash, gate_state, plan_required
from orch.core.ids import normalize_ref
from orch.core.lifecycle import unanswered_blocking
from orch.core.ops import claim_expired
from orch.core.tasks_check import task_findings
from orch.errors import OrchError

SYSTEM = Actor("agent", "orch-check", "check")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning" | "info" (an audit line, e.g. an agent's approval under delegation)
    code: str
    ticket: str | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def run_checks(ws, *, emit_events: bool = True) -> list[Finding]:
    findings = [Finding("error", "config", None, m) for m in validate_schema(ws.config)]
    findings += _check_addons(ws)
    findings += _check_artifact_mode(ws)
    findings += _check_trackers(ws)
    findings += _check_migration(ws)
    entries = store.scan(ws)
    findings += _check_entries(entries)
    findings += _check_event_log(ws)
    events = read_events(ws)
    for entry in entries:
        if entry.meta is None:
            continue
        try:
            ticket = store.read_ticket(entry.path)
        except (OrchError, UnicodeDecodeError):
            continue  # already reported as a parse finding
        closed = entry.status == "done" and _closed_by_human(events, ticket.id)
        findings += _check_ticket(ws, entry, ticket, events, emit_events, closed=closed)
        findings += _check_artifacts(ws, ticket)
        findings += [Finding(level, code, ticket.id, message)
                     for level, code, message in task_findings(ws, ticket, entry.status, entries)
                     if not (closed and code == "tasks-open-at-testing")]
    findings += _check_remote(ws, events)
    findings += _check_human_evidence(events)
    findings += _check_widgets(ws)
    if not ledger.head_ok():
        findings.append(Finding("error", "ledger-cut", None,
                                "the approval ledger on this machine is shorter than its signed head record, or ends "
                                "differently: entries were removed or replaced, so no chained decision (a done "
                                "verdict, a close, a workspace setting) counts as verified; stop and ask the human "
                                "to look at it"))
    html_state = ledger.widgets_html_state(ws)
    if html_state == "unsigned":
        findings.append(Finding("warning", "unsigned-setting", None,
                                "widgets.html is true in orchestrator/config.json, but no current signed decision for "
                                "this checkout on this machine backs it, so agent HTML stays off: the human turns it "
                                "on with `orch widget html on` in their own terminal"))
    elif html_state == "stale":
        findings.append(Finding("warning", "stale-setting", None,
                                "widgets.html is false in orchestrator/config.json, but the signed on is still in "
                                "force: run `orch widget html off` so that only a new human decision turns it on"))
    for cname in ledger.checks_digests(ws.config):
        cstate = ledger.check_state(ws, cname)
        if cstate in ("unsigned", "changed"):
            findings.append(Finding("warning", "unsigned-check", None,
                                    f"check {cname!r} in orchestrator/config.json is "
                                    + ("not signed by the human" if cstate == "unsigned"
                                       else "different from the version the human signed")
                                    + ", so a receipt made with it is marked as such: the human reviews it and runs "
                                      "`orch checks sign` in their own terminal"))
    findings += _check_orphan_artifacts(ws, entries)
    findings += _check_commits(ws, entries)
    return findings


def _check_widgets(ws) -> list[Finding]:
    """widget-parse / widget-schema / widget-place / widget-digest (docs/widgets.md), as `orch widget check`."""
    from orch.widgets import pages
    from orch.widgets.validate import findings
    return ([Finding(r["level"], r["code"], r["ticket"], f"{r['section']}, line {r['line']}: {r['message']}")
             for r in findings(ws)]
            + [Finding(r["level"], r["code"], None, f"{r['section']}:{r['line']}: {r['message']}")  # a wiki page
               for r in pages.findings(ws)])


def _record_invalidation(ws, t, gate: str, events) -> None:
    """One gate.invalidated event per gate and changed content (never a second one for the same hash)."""
    h = gate_hash(t, gate)
    if not any(e.ticket == t.id and e.kind == "gate.invalidated" and e.data.get("gate") == gate and e.data.get("hash") == h
               for e in events):
        append_event(ws, t.id, "gate.invalidated", SYSTEM, {"gate": gate, "hash": h})


def record_invalidations(ws, findings: list[Finding]) -> None:
    """The events `run_checks` would have written, for findings it computed with emit_events=False (the dashboard
    runs the checks on a timer, which never writes; the Workspace page that shows them records them)."""
    tids = {f.ticket.upper() for f in findings if f.code == "gate-invalidated" and f.ticket}
    if not tids:
        return
    events = read_events(ws)
    for entry in store.scan(ws):
        if entry.id.upper() not in tids or entry.meta is None:
            continue
        try:
            t = store.read_ticket(entry.path)
        except (OrchError, OSError, UnicodeDecodeError):
            continue
        for gate in GATE_SECTIONS:
            if ((t.meta.get("gates") or {}).get(gate) or {}).get("approved") and gate_state(t, gate) == "invalidated":
                _record_invalidation(ws, t, gate, events)
    store.forget_scope()  # the event log changed under this request


def record_ticket_invalidations(ws, t) -> None:
    """Write the gate.invalidated events a ticket is owed (an approved gate whose text changed since), once each.
    Every mutation calls this on the ticket it is about to change, before it applies, so the invalidation is on the
    audit trail ahead of any later decision on that ticket: read-only commands never write it (#108)."""
    gates = [g for g in GATE_SECTIONS
             if ((t.meta.get("gates") or {}).get(g) or {}).get("approved") and gate_state(t, g) == "invalidated"]
    if not gates:
        return
    events = read_events(ws)
    for gate in gates:
        _record_invalidation(ws, t, gate, events)


def _check_addons(ws) -> list[Finding]:
    """Addon findings from manifests and user files only: checks never import addon code."""
    from orch.addons.discovery import discover
    from orch.addons.userfiles import trust_state, workspace_addons

    out = []
    if ws.config.get("addons"):
        out.append(Finding("warning", "addon-config-ignored", None,
                           "config.addons is ignored — enable addons in Workspace & addons"))
    if (ws.home / "addons").exists():
        out.append(Finding("warning", "addon-workspace-folder", None,
                           "ignored: addons are not loaded from the workspace (orchestrator/addons/)"))
    found = {}
    for f in discover():
        found.setdefault(f.name, f)
    for name, value in sorted(workspace_addons(ws.root).items()):
        if not value["enabled"]:
            continue
        f = found.get(name)
        if f is None:
            out.append(Finding("warning", "addon-missing", None, f"addon '{name}' is enabled here but not installed"))
        elif (state := trust_state(f)) in ("untrusted", "changed"):
            out.append(Finding("warning", "addon-not-trusted", None,
                               f"addon '{name}' is enabled but {state}; it is not loaded until you trust it"))
        elif state != "trusted":  # invalid or could not be checked: trusting it would not help
            out.append(Finding("warning", "addon-not-trusted", None,
                               f"addon '{name}' is enabled but {state}; it is not loaded until you fix the addon"))
    return out


def _check_event_log(ws) -> list[Finding]:
    """Lines of events.jsonl that orch did not write as they stand (orch numbers events 1, 2, 3, ... under the
    events lock): skipped lines and accepted gaps, in file order. Every reader already handles them; this names
    them so the human can look."""
    scan = scan_events(ws)
    return [Finding(t.level, "event-log-tampered", None,
                    f"events.jsonl line {t.line}: {t.reason}" + (f" ({t.hint})" if t.hint else ""))
            for t in sorted((*scan.tampered, *scan.gaps), key=lambda t: t.line)]


def _check_artifact_mode(ws) -> list[Finding]:
    mode = ws.config["artifacts"]["mode"]
    if mode != "local":
        return [Finding("warning", "artifact-mode", None, f"artifacts.mode {mode!r} is ignored; artifacts are stored locally")]
    return []


def _check_trackers(ws) -> list[Finding]:
    from orch.core.trackers import tracker_problem
    out = []
    for i, t in enumerate(ws.config.get("external_trackers") or []):
        problem = tracker_problem(t) if isinstance(t, dict) else "must be an object {prefix, pattern, url}"
        if problem:
            name = t.get("prefix", "?") if isinstance(t, dict) else "?"
            out.append(Finding("error", "tracker-config", None, f"external_trackers[{i}] ({name}): {problem}"))
    return out


def _check_migration(ws) -> list[Finding]:
    """What `orch migrate` would change is an error (it is a command away); what it would refuse is a warning, since
    it needs a person. Old tickets and bare trackers also say so where they fail to load or fail `tracker_problem`."""
    from orch.core import migrate
    try:
        result = migrate.plan(ws.home)
    except (OSError, ValueError):
        return []  # an unreadable config is reported by validate_schema
    out = []
    for item in result.items:
        if item.ticket is not None:
            out.append(Finding("error", "needs-migration", item.ticket, f"{item.rel}: old format ({', '.join(item.rules)}): "
                               "run `orch migrate` (a dry run), then `orch migrate --apply`"))
            out += [Finding("warning", "approval-voided", item.ticket, n) for n in item.notes]
    for where, rule, why in result.refused:
        if where.startswith("tickets/done/"):
            why = f"history, cannot migrate ({rule}): {why}"
        else:
            why = f"needs a human decision ({rule}): {why}"
        out.append(Finding("warning", "migration-refused", None, f"{where}: {why}"))
    out += [Finding("warning", "approval-voided", None, f"{w}: {n}") for w, n in result.notes]
    return out


def _check_entries(entries) -> list[Finding]:
    out = []
    counts = Counter(e.id.upper() for e in entries)
    for e in entries:
        where = f"{e.path.parent.name}/{e.path.name}"
        if e.meta is None:
            out.append(Finding("error", "parse", e.id, f"{where}: {e.error}"))
            continue
        if counts[e.id.upper()] > 1:
            out.append(Finding("error", "duplicate", e.id, f"{where} shares its id with another file"))
        m = store.FILENAME_RE.match(e.path.name)
        if m and m.group(1).upper() != str(e.meta.get("id", "")).upper():
            out.append(Finding("error", "filename", e.id, f"{where}: filename does not match id {e.meta.get('id')}"))
        if e.meta.get("status") != e.status:
            out.append(Finding("error", "status-mismatch", e.id, f"{where}: in folder {e.status} but status is {e.meta.get('status')!r}"))
    return out


def _is_human(e) -> bool:
    return str(e.actor).startswith("human:")


def _latest_human(events, ticket_id: str, kind: str, key: str, value):
    """The most recent human `kind` event on the ticket whose data[key] == value, or None."""
    for e in reversed(events):
        if e.ticket == ticket_id and e.kind == kind and _is_human(e) and e.data.get(key) == value:
            return e
    return None


def _delegated_event(events, ticket_id: str, gate: str, g: dict):
    """The agent's `gate.delegated` event that recorded this gate's approval under delegation, or None."""
    for e in reversed(events):
        if (e.ticket == ticket_id and e.kind == "gate.delegated" and e.data.get("gate") == gate
                and e.data.get("hash") == g.get("hash") and e.data.get("delegation") == g.get("delegation")):
            return e
    return None


def _latest_status_change(events, ticket_id: str):
    for e in reversed(events):
        if e.ticket == ticket_id and "to" in e.data:
            return e
    return None


def _closed_by_human(events, ticket_id: str) -> bool:
    """The ticket's last status change is a human `close` to done (Ops.close, spec v2 §13.2): a valid way to done
    that skips the gates, the plan and the verdict on purpose."""
    last = _latest_status_change(events, ticket_id)
    return bool(last and last.kind == "ticket.moved" and _is_human(last) and last.data.get("command") == "close"
                and last.data.get("to") == "done")


def _check_ticket(ws, entry, t, events, emit: bool, *, closed: bool = False) -> list[Finding]:
    out = []
    tid = t.id
    signed = ledger.entries(ws)  # cached per file; read once per ticket here
    for gate in GATE_SECTIONS:
        g = (t.meta.get("gates") or {}).get(gate) or {}
        if not g.get("approved"):
            continue
        approval = _latest_human(events, tid, "gate.approved", "gate", gate)
        delegated = _delegated_event(events, tid, gate, g) if g.get("delegation") else None
        if delegated is not None and (approval is None or approval.data.get("hash") != g.get("hash")):
            approval = delegated  # an agent's approval under the epic's delegation: audited below
        if approval is None or approval.data.get("hash") != g.get("hash"):
            out.append(Finding("error", "unverified-approval", tid, f"{gate} gate is marked approved but no human approval of this content was recorded (edited by hand?)"))
        verification = ledger.gate_verification(ws, t, gate, signed, events=events)
        if verification == "delegated":
            out.append(Finding("info", "delegated-approval", tid,
                               f"the {gate} was auto-approved by {delegated.actor} under the delegation of epic "
                               f"{delegated.data.get('epic')} at {delegated.at} (not a human decision)"))
        if verification == "unverified":
            out.append(Finding("warning", "unsigned-decision", tid,
                               f"the {gate} approval is not in the ledger on this machine (approved elsewhere, before "
                               f"the ledger, or written by hand); agents cannot proceed on it: review it with "
                               f"`orch ledger adopt {tid}`"))
        if gate_state(t, gate) == "invalidated":
            out.append(Finding("warning", "gate-invalidated", tid, f"{gate} changed since it was approved on {g['approved']}; needs re-approval"))
            if emit:
                _record_invalidation(ws, t, gate, events)
    if not closed and entry.status != "backlog" and gate_state(t, "requirements") == "pending":
        out.append(Finding("error", "status-without-gate", tid, f"ticket is {entry.status} but its requirements were never approved"))
    if not closed and entry.status in ("testing", "done") and plan_required(ws, t) and gate_state(t, "plan") != "approved":
        out.append(Finding("error", "status-without-plan", tid, f"ticket is {entry.status} but its plan is {gate_state(t, 'plan')}"))
    if entry.status == "done" and ledger.done_verification(ws, t, closed=closed, signed=signed, events=events) == "unverified":
        out.append(Finding("warning", "unsigned-decision", tid,
                           f"the ticket's done verdict or close is not in the ledger on this machine: review it with "
                           f"`orch ledger adopt {tid}`"))
    if entry.status == "done" and ledger.pre_chain(ws, t, signed, events, closed=closed):
        out.append(Finding("warning", "pre-chain-signature", tid,
                           f"the done was signed before the ledger chain (weaker verification); review with "
                           f"`orch ledger adopt {tid}`"))
    for q in t.meta.get("questions") or []:
        if isinstance(q, dict) and ledger.answer_verification(ws, t, q, signed) == "unverified":
            out.append(Finding("warning", "unsigned-decision", tid,
                               f"the answer to {q.get('id')} is not in the ledger on this machine: review it with "
                               f"`orch ledger adopt {tid}`"))
    if entry.status == "done" and not closed:
        last = _latest_status_change(events, tid)
        if not (last and last.kind == "verdict.given" and _is_human(last)
                and last.data.get("verdict") == "done" and last.data.get("to") == "done"):
            out.append(Finding("error", "unverified-verdict", tid, "ticket is done but its last recorded status change is not a human done verdict"))
    for q in t.meta.get("questions") or []:
        if q.get("answer") in (None, ""):
            continue
        answered = _latest_human(events, tid, "question.answered", "qid", q.get("id"))
        if answered is None or answered.data.get("answer") != q.get("answer"):
            out.append(Finding("error", "unverified-answer", tid, f"{q.get('id')} has an answer that no recorded human answer matches"))
    unproven = evidence.ticked_without_evidence(t)
    if unproven:
        out.append(Finding("warning", "criterion-ticked-without-evidence", tid,
                           ", ".join(f"AC{n}" for n in unproven) + " ticked but no Verification line cites it"))
    blocking = unanswered_blocking(t)
    if entry.status == "waiting" and not blocking:
        out.append(Finding("warning", "waiting-without-question", tid, "ticket is waiting but has no unanswered blocking question"))
    if entry.status == "in-progress" and blocking:
        out.append(Finding("warning", "blocking-question-open", tid, "blocking questions are open while in progress: " + ", ".join(str(q.get("id")) for q in blocking)))
    out += _check_log_lines(t, events)
    claim = t.meta.get("claim") or {}
    if claim.get("session") and claim_expired(claim, float(ws.config["claims"]["ttl_hours"])):
        out.append(Finding("warning", "claim-expired", tid, f"claim by {claim.get('harness')} since {claim.get('at')} has expired"))
    return out


def _check_remote(ws, events) -> list[Finding]:
    """A ticket event that claims to come from a phone must have been written by a verified, ledgered decision."""
    from orch.remote import ledger
    phone_events = [e for e in events if str(e.via).startswith("phone:")]
    if not phone_events:
        return []
    from orch.core import ledger as signed_ledger
    seqs, signed = ledger.applied_seqs(ws), signed_ledger.entries(ws)
    out = []
    for e in phone_events:
        if e.seq not in seqs:
            out.append(Finding("error", "unverified-remote", e.ticket, f"event {e.seq} ({e.kind}) claims to come from "
                                                                      f"{e.via} but no verified phone decision recorded it"))
        elif signed_ledger.phone_event_signed(e, signed) is False:
            out.append(Finding("error", "unverified-remote", e.ticket, f"event {e.seq} ({e.kind}) from {e.via} is not "
                                                                      "in the approval ledger on this machine"))
    return out


# `- 2026-10-04T10:00Z [you] approved plan`: a Log line orch writes for a human action (orch.core.events.log_line).
_HUMAN_LOG_LINE = re.compile(r"^\s*-\s+(\d{4}-\d\d-\d\dT\d\d:\d\dZ)\s+\[\s*(?:you|human)\b[^\]]*\]\s*(.*)$", re.I)


def _check_log_lines(t, events) -> list[Finding]:
    """#21: every Log line that claims a human action needs a human event on the ticket written in the same minute
    (or the next: the line is stamped just before the event). Each event backs at most one line."""
    from datetime import timedelta

    from orch.clock import parse_stamp
    claims = [(line, m) for line in t.section("Log").split("\n") if (m := _HUMAN_LOG_LINE.match(line))]
    if not claims:
        return []
    times = []
    for e in events:
        if e.ticket == t.id and _is_human(e):
            try:
                times.append(parse_stamp(e.at).replace(second=0))
            except ValueError:
                continue
    out = []
    for line, m in claims:
        try:
            at = parse_stamp(m.group(1))
        except ValueError:
            at = None
        match = next((x for x in times if at is not None and timedelta(0) <= x - at <= timedelta(minutes=1)), None)
        if match is None:
            out.append(Finding("error", "unverified-log-line", t.id,
                               f"Log line claims a human action that no recorded human event backs: {line.strip()[:120]}"))
        else:
            times.remove(match)
    return out


def _check_human_evidence(events) -> list[Finding]:
    """#19: a human event written by a process that ran under an agent harness was not the human's."""
    out = []
    for e in events:
        harness = (e.evidence or {}).get("harness") if _is_human(e) else None
        if harness:
            out.append(Finding("error", "human-action-from-agent", e.ticket,
                               f"event {e.seq} ({e.kind}) is recorded as a human action, but an agent harness "
                               f"({harness}) ran the process that wrote it"))
    return out


def _check_artifacts(ws, t) -> list[Finding]:
    """Artifacts the human cannot reach from the ticket: files in its folder that it does not link, linked files
    changed or gone since they were added, malformed entries, inline references without alt text or to nothing,
    and (while the ticket is worked on) URLs and file names in Log or Verification that are not linked."""
    from orch.core import artifacts as art
    out = []
    raw = t.meta.get("artifacts")
    if raw is not None and (not isinstance(raw, list) or len(art.entries(t)) != len(raw)):
        out.append(Finding("warning", "artifact-entry", t.id,
                           "the frontmatter `artifacts` list holds entries orch cannot read (one of name, url or "
                           "static each; http(s) URLs only; relative names without ..)"))
    loose = art.unregistered(ws, t)
    if loose:
        out.append(Finding("warning", "artifact-unlinked", t.id,
                           f"{len(loose)} file(s) in artifacts/{t.id}/ not linked in the ticket: " + ", ".join(loose[:5])
                           + f" (orch artifact scan {t.id})"))
    loose_static = art.unregistered_static(ws, t)
    if loose_static:
        out.append(Finding("warning", "artifact-unlinked", t.id,
                           f"{len(loose_static)} file(s) in static/{t.id}/ named nowhere in the ticket: "
                           + ", ".join(loose_static[:5]) + f" (orch artifact scan {t.id})"))
    for name, state in art.changed(ws, t):
        out.append(Finding("warning", "artifact-changed", t.id,
                           f"artifact {name} is {'gone' if state == 'missing' else 'changed since it was added'}; "
                           f"re-add it with `orch artifact add {t.id} <file> --replace` so the change is visible"))
    found = [r for text in t.sections.values() for r in art.refs(text, t.id)]
    for kind, name, alt in found:
        if kind == "image" and not alt.strip():
            out.append(Finding("warning", "artifact-image-alt", t.id,
                               f"inline image artifact:{name} has no alt text; write ![what it shows](artifact:{name})"))
    for name in dict.fromkeys(name for _, name, _ in found):
        if art.find(t, name) is None:
            out.append(Finding("warning", "artifact-ref-missing", t.id,
                               f"artifact:{name} is referenced but not linked; add it with `orch artifact add`"))
    if t.status in ("in-progress", "waiting", "testing"):
        cited = art.mentions(t)
        if cited:
            out.append(Finding("warning", "artifact-mention", t.id,
                               "Log or Verification names evidence the ticket does not link: " + ", ".join(cited[:5])
                               + f" (orch artifact add {t.id} <file> | --url <link>)"))
    return out


def _check_orphan_artifacts(ws, entries) -> list[Finding]:
    if not ws.artifacts_dir.is_dir():
        return []
    ids = {e.id.upper() for e in entries}
    return [
        Finding("warning", "orphan-artifacts", d.name, f"artifacts/{d.name} has no matching ticket")
        for d in sorted(ws.artifacts_dir.iterdir())
        if d.is_dir() and d.name.upper() not in ids
    ]


_SINCE = re.compile(r"\d{4}-\d{2}-\d{2}|[0-9a-fA-F]{7,40}")


def commits_since(cfg: dict, repo: str) -> str | None:
    """The baseline of repo's commit checks (#165): `check.since`, one date (YYYY-MM-DD) or commit for every repo,
    or a map of repo name to one. `orch init` sets today; without it every recent commit is checked, as before."""
    since = (cfg.get("check") or {}).get("since") if isinstance(cfg.get("check"), dict) else None
    if isinstance(since, dict):
        since = since.get(repo)
    return since if isinstance(since, str) and _SINCE.fullmatch(since) else None


def _check_commits(ws, entries, limit: int = 200) -> list[Finding]:
    repos = ws.config["git"].get("repos") or {}
    if not repos:
        return []
    out = []
    known = {e.id.upper() for e in entries}
    linked = {
        str(x.get("key", "")).upper()
        for e in entries if e.meta
        for x in e.meta.get("external") or [] if isinstance(x, dict)
    }
    local_re = re.compile(rf"\b{re.escape(ws.config['id']['prefix'])}-\d+\b", re.IGNORECASE)
    from orch.core.trackers import plain
    trackers = []
    for t in ws.config["external_trackers"]:
        try:
            trackers.append(re.compile(rf"\b(?:{plain(t['pattern'])})\b", re.IGNORECASE))
        except (re.error, TypeError, KeyError):
            continue  # _check_trackers reports it: tracker_problem compiles this same plain() form
    for name, repo in repos.items():
        path = (ws.root / ((repo or {}).get("path") or name)).resolve()
        if not (path / ".git").exists():
            continue
        since = commits_since(ws.config, name)
        span = [f"--since={since} 00:00:00"] if since and "-" in since else [f"{since}..HEAD"] if since else []
        try:
            res = subprocess.run(["git", "-C", str(path), "log", f"-n{limit}", "--format=%h %s", *span, "--"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            out.append(Finding("warning", "git-unavailable", None, f"{name}: could not read git log ({e})"))
            continue
        if res.returncode != 0:
            if since and "-" not in since:
                out.append(Finding("warning", "check-since", None, f"{name}: check.since commit {since} is not in "
                                   "this repo, so its commits were not checked; set a commit of this repo or a date"))
            continue
        for line in res.stdout.splitlines():
            sha, _, subject = line.partition(" ")
            for m in local_re.finditer(subject):
                if normalize_ref(ws, m.group(0)).upper() not in known:
                    out.append(Finding("error", "commit-unknown-ticket", None, f"{name}@{sha} cites {m.group(0)}, which does not exist"))
            for rx in trackers:
                for m in rx.finditer(subject):
                    if m.group(0).upper() not in linked:
                        out.append(Finding("warning", "commit-unlinked-key", None, f"{name}@{sha} cites {m.group(0)}, which no ticket links"))
    return out
