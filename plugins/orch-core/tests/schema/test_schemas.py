import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from orch import schema
from orch.schema import SchemaError

from . import doc_coverage as cov
from . import examples as ex

DOC = Path(__file__).resolve().parents[4] / "docs" / "architecture" / "orch-v2-ticket-format.md"


def bad(name, obj, path, contains=""):
    with pytest.raises(SchemaError) as e:
        schema.validate(name, obj)
    assert e.value.path == path, e.value
    assert contains in e.value.message, e.value


def mut(base, fn):
    o = ex.fresh(base)
    fn(o)
    return o


# ---- meta ----
@pytest.mark.parametrize("name", schema.names())
def test_schema_is_valid_json_schema(name):
    Draft202012Validator.check_schema(schema.load(name))


def test_loader_basics():
    assert "ticket" in schema.names() and schema.names() == sorted(schema.names())
    s = schema.load("ticket")
    s["x"] = 1
    assert "x" not in schema.load("ticket")
    with pytest.raises(KeyError):
        schema.load("nope")


@pytest.mark.parametrize("name,obj", list(ex.VALID.items()))
def test_valid_examples(name, obj):
    schema.validate(name, obj)


@pytest.mark.parametrize("t,obj", list(ex.EVENTS.items()))
def test_valid_events(t, obj):
    schema.validate("event", obj)
    schema.validate("event." + t, obj)


def test_every_event_schema_has_an_example():
    assert {n[len("event.") :] for n in schema.names() if n.startswith("event.")} == set(ex.EVENTS)


def test_addon_event_envelope_only():
    schema.validate("event", ex.ADDON_EVENT)
    bad("event", mut(ex.ADDON_EVENT, lambda e: e.pop("host_sig")), "", "host_sig")
    bad("event", mut(ex.ADDON_EVENT, lambda e: e.update(type="gate.bogus")), "/type", "unknown core event type")
    bad("event", mut(ex.ADDON_EVENT, lambda e: e.update(type="nodot")), "/type", "unknown core event type")


# ---- parse / text rules ----
def test_duplicate_keys_refused():
    with pytest.raises(ValueError, match="duplicate key"):
        schema.parse_json('{"a": 1, "a": 2}')
    assert schema.parse_json('{"a": [1, {"b": 2}]}') == {"a": [1, {"b": 2}]}


def test_text_rules():
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\rb")), "/title", "LF")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="é")), "/title", "NFC")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e["receipt"].update(ms=1.5)), "/receipt/ms", "floats")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e["receipt"].update(ms=1.0)), "/receipt/ms", "floats")


# ---- workspace ----
def test_workspace_invalid():
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.pop("host")), "", "host")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.update(extra=1)), "", "extra")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["members"][0].update(role="boss")), "/members/0/role")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(count=0)), "/gates/plan/count")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(count="1")), "/gates/plan/count")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(who="x")), "/gates/plan", "who")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["workspace"].update(id="nope")), "/workspace/id")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["workspace"].update(prefix="demo")), "/workspace/prefix")
    bad(
        "workspace", mut(ex.WORKSPACE, lambda o: o["addons"]["dashboard"].update(extra=1)), "/addons/dashboard", "extra"
    )
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.update(schema="orch.workspace/1")), "/schema")


def test_keys_line_invalid():
    bad("keys-line", {"key": "DEMO-0043"}, "", "uid")
    bad("keys-line", {**ex.KEYS_LINE, "x": 1}, "", "x")
    bad("keys-line", {**ex.KEYS_LINE, "key": "demo-1"}, "/key")


# ---- ticket ----
def test_ticket_invalid():
    bad("ticket", mut(ex.TICKET, lambda t: t.pop("uid")), "", "uid")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(state="open")), "", "state")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(type="story")), "/type")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels="dbt")), "/labels")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(uid="short")), "/uid")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"][0].update(id="X1")), "/acceptance/0/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].pop("proves")), "/tasks/0", "proves")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].update(status="done")), "/tasks/0", "status")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(blocking="yes")), "/questions/0/blocking")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"].update(extra=[])), "/links", "extra")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(visibility="secret")), "/visibility")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(due="tomorrow")), "/due")
    bad("ticket", mut(ex.TICKET, lambda t: t["addons"].update({"Bad Name": {}})), "/addons")


def test_ticket_visibility_restricted_and_addons_open():
    t = mut(ex.TICKET, lambda t: t.update(visibility={"restricted": ["p_sev", "p_mara"]}, due="2026-11-01"))
    t["addons"]["estimate"]["anything"] = {"nested": [1, "x"]}
    schema.validate("ticket", t)
    bad("ticket", mut(ex.TICKET, lambda t: t.update(visibility={"restricted": []})), "/visibility/restricted")


def test_ticket_cross_field_and_size():
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][1].update(proves=["AC9"])), "/tasks/1/proves/0", "AC9")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"].append({"id": "AC1", "text": "dup"})), "/acceptance/3/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(recommended="zzz")), "/questions/0/recommended")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(parent="DEMO-0043")), "/key")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"].append({"id": "AC4", "text": "x" * 300_000})), "", "exceeds")


# ---- body ----
def test_body_invalid_and_types():
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].pop("plan")), "/sections", "plan")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(findings="x")), "/sections")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b.update(type="story")), "/type")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan=3)), "/sections/plan")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="x" * 70_000)), "/sections/plan", "exceeds")
    chore = {"type": "chore", "sections": {"requirements": "r", "plan": "p", "current_state": "c"}}
    schema.validate("body", chore)
    bad("body", {**chore, "sections": {**chore["sections"], "out_of_scope": "x"}}, "/sections")  # "-" for chore
    spike = {
        "type": "spike",
        "sections": {k: "x" for k in "context requirements plan decisions findings current_state".split()},
    }
    schema.validate("body", spike)
    bad("body", {**spike, "sections": {**spike["sections"], "verification": "x"}}, "/sections")
    epic = {
        "type": "epic",
        "sections": {k: "x" for k in "summary context requirements out_of_scope decisions current_state".split()},
    }
    schema.validate("body", epic)
    bad("body", {**epic, "sections": {**epic["sections"], "plan": "x"}}, "/sections")
    schema.validate(
        "body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update({"estimate.notes": "x", "summary": "s"}))
    )


# ---- events ----
def test_envelope_invalid():
    g = ex.EVENTS["gate.approved"]
    for k in ("v", "id", "seq", "at", "type", "actor", "based_on", "prev", "hash_v", "host_sig", "sig", "presence"):
        bad("event", mut(g, lambda e, k=k: e.pop(k)), "", k)
    bad("event", mut(g, lambda e: e.update(v=1)), "/v")
    bad("event", mut(g, lambda e: e.update(seq=0)), "/seq")
    bad("event", mut(g, lambda e: e.update(seq="5")), "/seq")
    bad("event", mut(g, lambda e: e.update(at="2026-10-09 09:10:11")), "/at")
    bad("event", mut(g, lambda e: e.update(at="2026-10-09T09:10:11.5Z")), "/at")
    bad("event", mut(g, lambda e: e.update(prev="sha256:zz")), "/prev")
    bad("event", mut(g, lambda e: e.update(host_sig="rsa:abc")), "/host_sig")
    bad("event", mut(g, lambda e: e.update(extra=1)), "", "extra")
    bad("event", mut(g, lambda e: e["actor"].update(device=None)), "/actor/device")
    bad("event", mut(g, lambda e: e.update(presence="none")), "/presence")
    bad("event", mut(g, lambda e: e.update(id="01J9ZP")), "/id")


def test_first_event_prev_null():
    schema.validate("event", mut(ex.EVENTS["task.done"], lambda e: e.update(prev=None, based_on=None)))


def test_agent_actor_rules():
    t = ex.EVENTS["task.done"]
    bad("event", mut(t, lambda e: e["actor"].pop("grant")), "/actor")  # neither grant nor unattended
    bad("event", mut(t, lambda e: e["actor"].update(unattended=True)), "/actor")  # both
    bad("event", mut(t, lambda e: e.update(sig=ex.SIG)), "", "sig")  # agents never sign
    schema.validate("event", ex.EVENTS["log"])  # unattended agent, no grant/for


def test_human_events_need_signature_and_person():
    for t in (
        "gate.approved",
        "gate.changes_requested",
        "verdict.given",
        "question.answered",
        "ticket.closed",
        "ticket.reopened",
        "people.changed",
        "member.added",
        "member.removed",
        "role.changed",
        "policy.changed",
        "addon.granted",
        "addon.purged",
        "session.granted",
        "restore",
    ):
        e = ex.EVENTS[t]
        bad("event", mut(e, lambda o: o.pop("sig")), "", "sig")
        bad("event", mut(e, lambda o: o.update(actor=ex.AGENT)), "")
    # an agent event must not carry a signature
    bad("event", mut(ex.EVENTS["handoff"], lambda e: e.update(sig=ex.SIG)), "")


def test_event_payload_invalid():
    E = ex.EVENTS
    bad("event", mut(E["gate.approved"], lambda e: e.pop("hash")), "", "hash")
    bad("event", mut(E["gate.approved"], lambda e: e.update(gate="Plan")), "/gate")
    bad("event", mut(E["gate.approved"], lambda e: e.update(hash="abc")), "/hash")
    bad("event", mut(E["gate.approved"], lambda e: e.update(bogus=1)), "", "bogus")
    bad("event", mut(E["task.done"], lambda e: e.pop("task")), "", "task")
    bad("event", mut(E["task.done"], lambda e: e["receipt"].pop("exit")), "/receipt", "exit")
    bad("event", mut(E["task.done"], lambda e: e["receipt"].update(extra=1)), "/receipt", "extra")
    bad("event", mut(E["task.done"], lambda e: e.update(task="3")), "/task")
    bad("event", mut(E["verdict.given"], lambda e: e.update(outcome="maybe")), "/outcome")
    bad("event", mut(E["claim.taken"], lambda e: e["takeover"].pop("reason")), "/takeover", "reason")
    bad("event", mut(E["claim.released"], lambda e: e.update(reason="x")), "/reason")
    bad("event", mut(E["edit.external"], lambda e: e.update(files=["x.txt"])), "/files/0")
    bad("event", mut(E["edit.external"], lambda e: e.update(actor=ex.AGENT)), "")
    bad("event", mut(E["edit.external"], lambda e: e.update(sig=ex.SIG)), "")
    bad("event", mut(E["people.changed"], lambda e: e.pop("add")), "")  # needs add or remove
    bad("event", mut(E["people.changed"], lambda e: e.update(role="owners")), "/role")
    bad("event", mut(E["member.added"], lambda e: e.update(role="root")), "/role")
    bad("event", mut(E["addon.granted"], lambda e: e.update(package_sha256="zz")), "/package_sha256")
    bad("event", mut(E["restore"], lambda e: e.update(from_seq=-1)), "/from_seq")
    bad("event", mut(E["policy.changed"], lambda e: e["gates"]["plan"].update(count=0)), "/gates/plan/count")


def test_artifact_events():
    A = ex.EVENTS["artifact.added"]
    bad("event", mut(A, lambda e: e.pop("sha256")), "")
    bad("event", mut(A, lambda e: e.update(kind="video")), "/kind")
    bad("event", mut(A, lambda e: e.update(bytes="1")), "/bytes")
    bad("event", mut(A, lambda e: e.update(name="../x")), "/name")
    bad("event", mut(A, lambda e: e.update(extra=1)), "", "extra")
    bad("event", mut(A, lambda e: e.update(ac="X")), "/ac")
    # an addon artifact has addon + ref instead of a file
    addon = mut(
        A, lambda e: [e.pop(k) for k in ("sha256", "bytes")] and e.update(kind="board", addon="dashboard", ref="b/1")
    )
    schema.validate("event", addon)
    bad("event", mut(addon, lambda e: e.update(sha256="3f9a" * 16)), "")
    # feedback may only be added by a human
    fb = mut(A, lambda e: e.update(kind="feedback"))
    bad("event", fb, "/actor/kind")
    fb_h = mut(fb, lambda e: e.update(actor=ex.PERSON))
    schema.validate("event", fb_h)


def test_artifact_entry():
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.pop("actor")), "", "actor")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(kind="movie")), "/kind")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(extra=1)), "", "extra")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(sha256="sha256:" + "ab" * 32)), "/sha256")
    schema.validate("artifact", ex.ARTIFACT_ADDON)
    bad("artifact", mut(ex.ARTIFACT_ADDON, lambda a: a.pop("ref")), "")


# ---- checkpoint / manifest / operation / cli ----
def test_checkpoint_invalid():
    schema.validate("checkpoint", ex.CHECKPOINT_WORKSPACE)
    bad("checkpoint", mut(ex.CHECKPOINT_TICKET, lambda c: c.pop("head")), "")
    bad("checkpoint", mut(ex.CHECKPOINT_TICKET, lambda c: c.update(seq=0)), "/seq")


def test_manifest_invalid():
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m.pop("name")), "", "name")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m.update(hooks=[])), "", "hooks")
    bad(
        "addon-manifest",
        mut(ex.MANIFEST, lambda m: m["fields"]["points"].update(set_by=["root"])),
        "/fields/points/set_by/0",
    )
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m["fields"]["points"].pop("set_by")), "/fields/points", "set_by")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m["fields"]["points"].update(type="float")), "/fields/points/type")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m["sections"][0].update(types=["story"])), "/sections/0/types/0")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m.update(agents_md="a\nb")), "/agents_md")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m.update(skills=["../x"])), "/skills/0")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m.update(capabilities="pty")), "/capabilities")
    bad("addon-manifest", mut(ex.MANIFEST, lambda m: m["cli"]["ops"].append({"name": "x"})), "/cli/ops/0")


def test_operation_invalid():
    for k in cov.DOC_NAMED_OPERATION_FIELDS:
        bad("operation", mut(ex.OPERATION, lambda o, k=k: o.pop(k)), "", k)
    bad("operation", mut(ex.OPERATION, lambda o: o.update(who="root")), "/who")
    bad("operation", mut(ex.OPERATION, lambda o: o.update(extra=1)), "", "extra")
    bad("operation", mut(ex.OPERATION, lambda o: o.update(emits="task.done")), "/emits")
    bad("operation", mut(ex.OPERATION, lambda o: o["input"].update(type="array")), "/input/type")
    bad("operation", mut(ex.OPERATION, lambda o: o["input"].update(properties=5)), "/input/properties")
    bad("operation", mut(ex.OPERATION, lambda o: o["output"].update(text="done")), "/output/text")
    bad("operation", mut(ex.OPERATION, lambda o: o["output"].update(text="ok a\nnext: b\nnext: c")), "/output/text")
    bad("operation", mut(ex.OPERATION, lambda o: o["errors"][0].pop("fix")), "/errors/0", "fix")
    bad("operation", mut(ex.OPERATION, lambda o: o["errors"][0]["fix"].update(argv=[])), "/errors/0/fix/argv")


def test_cli_envelopes_invalid():
    bad("cli-result", mut(ex.CLI_RESULT, lambda r: r.update(ok=False)), "/ok")
    bad("cli-result", mut(ex.CLI_RESULT, lambda r: r.pop("hints")), "", "hints")
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r["error"].pop("code")), "/error", "code")
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r["error"].update(retryable="no")), "/error/retryable")
    bad("wait-result", {**ex.WAIT_RESULT, "kind": "done"}, "/kind")
    bad("wait-result", {"kind": "answered"}, "", "cursor")


# ---- doc coverage ----
def test_every_schema_is_mapped_to_the_doc():
    non_event = {n for n in schema.names() if not n.startswith("event.")}
    assert non_event == set(cov.SCHEMA_TO_DOC)


def test_event_types_match_doc_tables():
    have = {n[len("event.") :] for n in schema.names() if n.startswith("event.")}
    assert have == set(cov.DOC_NAMED_EVENTS) | set(cov.DOC_IMPLIED_EVENTS)
    assert not set(cov.DOC_NAMED_EVENTS) & set(cov.DOC_IMPLIED_EVENTS)


def test_doc_named_events_appear_in_the_doc():
    text = DOC.read_text()
    for t in cov.DOC_NAMED_EVENTS:
        assert re.search(rf"`{re.escape(t)}\b|\"type\":\"{re.escape(t)}\"|\b{re.escape(t)} \{{", text), t


def test_doc_event_types_in_text_have_schemas():
    """Every dotted lowercase `x.y` token in backticks in the doc that looks like an event is known or listed."""
    text = DOC.read_text()
    found = set(re.findall(r'"type":"([a-z.]+)"', text))
    found |= set(
        re.findall(r"`((?:gate|task|artifact|edit|projection|member|role|policy|addon|claim|question)\.[a-z_]+)`", text)
    )
    known = {n[len("event.") :] for n in schema.names() if n.startswith("event.")}
    # the doc also writes generic placeholders and non-event tokens
    ignore = {"artifact.added"} - known
    assert not (found - known - ignore), found - known


def test_doc_named_fields_exist():
    env = schema.load("event")["properties"]
    for f in cov.DOC_NAMED_ENVELOPE_FIELDS:
        assert f in env, f
    t = schema.load("ticket")["properties"]
    assert set(t) == set(cov.DOC_NAMED_TICKET_FIELDS)
    art = schema.load("artifact")
    fields = set(art["properties"])
    for b in (art["$defs"]["fields"]["then"], art["$defs"]["fields"]["else"]):
        fields |= set(b["properties"])
    assert set(cov.DOC_NAMED_ARTIFACT_FIELDS) <= fields
    assert set(cov.DOC_NAMED_CORE_ARTIFACT_KINDS) == set(schema.load("common")["$defs"]["coreArtifactKind"]["enum"])
    assert set(cov.DOC_NAMED_MANIFEST_KEYS) <= set(schema.load("addon-manifest")["properties"])
    assert set(cov.DOC_NAMED_OPERATION_FIELDS) == set(schema.load("operation")["properties"])


def test_doc_mentions_what_we_map():
    """Guard against the doc dropping a section we cite."""
    text = DOC.read_text()
    for needle in ("## 3. `ticket.json`", "## 4. `body.md`", "## 5. `events.jsonl`", "## 6. Artifacts", "## 8. Addons"):
        assert needle in text
    json.dumps(cov.SCHEMA_TO_DOC)
