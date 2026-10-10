import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from orch import canon, schema
from orch.schema import SchemaError

from . import doc_coverage as cov
from . import examples as ex

DOC = Path(__file__).resolve().parents[4] / "docs" / "architecture" / "orch-v2-ticket-format.md"
TICKET_LOG_TYPES = set(schema.event_types("ticket"))
WS_LOG_TYPES = set(schema.event_types("workspace"))


def bad(name, obj, path, contains="", **kw):
    with pytest.raises(SchemaError) as e:
        schema.validate(name, obj, **kw)
    assert e.value.path == path, e.value
    assert contains in e.value.message, e.value


def mut(base, fn):
    o = ex.fresh(base)
    fn(o)
    return o


def log_of(t):
    return "workspace" if t in ex.WORKSPACE_ONLY else "ticket"


def ev(t, **changes):
    """A fresh copy of the example event of type ``t`` with top-level changes (None removes a key)."""
    e = ex.fresh(ex.EVENTS[t])
    for k, v in changes.items():
        if v is None and k in e:
            del e[k]
        else:
            e[k] = v
    return e


# ---------------------------------------------------------------- meta
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
    with pytest.raises(KeyError):
        schema.validate("nope", {})


def test_no_pattern_ends_in_a_bare_dollar():
    """Python's `$` also matches before a final LF; every pattern must use (?![\\s\\S]) instead."""

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "pattern":
                    assert not v.endswith("$"), (where, v)
                    assert v.endswith("(?![\\s\\S])") or not v.startswith("^"), (where, v)
                walk(v, where)
        elif isinstance(node, list):
            for v in node:
                walk(v, where)

    for n in schema.names():
        walk(schema.load(n), n)


@pytest.mark.parametrize(
    "ref,value",
    [
        ("hash", "sha256:" + "a" * 64 + "\n"),
        ("ticketKey", "DEMO-0043\n"),
        ("personId", "p_" + ex.PID + "\n"),
        ("ulid", ex.U1 + "\n"),
        ("line", "one line\n"),
        ("timestamp", "2026-10-09T09:10:11Z\n"),
    ],
)
def test_trailing_lf_is_refused_by_the_patterns(ref, value):
    assert not _def_valid(ref, value)
    if ref != "line":
        assert _def_valid(ref, value[:-1])
    bad("keys-line", {**ex.KEYS_LINE, "key": "DEMO-0043\n"}, "/key")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e.update(prev=ex.H2 + "\n")), "/prev")


@pytest.mark.parametrize("name,obj", list(ex.VALID.items()))
def test_valid_examples(name, obj):
    schema.validate(name, obj)


@pytest.mark.parametrize("key", list(ex.EVENTS))
def test_valid_events(key):
    obj = ex.EVENTS[key]
    schema.validate("event", obj, log=log_of(obj["type"]))
    schema.validate("event." + obj["type"], obj)
    schema.validate("event", obj)


@pytest.mark.parametrize("t", list(ex.EVENTS_WS_VARIANTS))
def test_shared_types_in_the_workspace_log(t):
    e = ex.EVENTS_WS_VARIANTS[t]
    schema.validate("event", e, log="workspace")
    schema.validate("event", e)
    bad("event", e, "", "ws_seq", log="ticket")
    bad("event", ex.EVENTS[t], "/ws_seq", "no ws_seq", log="workspace")


def test_every_event_schema_has_an_example_and_back():
    have = {n[len("event.") :] for n in schema.names() if n.startswith("event.")}
    assert have == {e["type"] for e in ex.EVENTS.values()}


def test_new_ticket_defaults_are_valid():
    schema.validate("ticket", ex.TICKET_NEW)
    schema.validate("artifact", ex.ARTIFACT_ADDON)
    schema.validate("checkpoint", ex.CHECKPOINT_WORKSPACE)


# ---------------------------------------------------------------- parser differential, text rules, limits
def test_parse_json_is_the_canonical_strict_parser():
    cases = [
        '{"a": 1, "a": 2}',  # duplicate key
        '{"a": 1.0}',  # float
        '{"a": 1e3}',
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": 9007199254740992}',  # unsafe int
        '{"é": 1}',  # non-ASCII key
        '{"": 1}',  # empty key
        '{"a": "\\ud800"}',  # lone surrogate
        "[" * 17 + "]" * 17,  # too deep
        '{"a": 1} x',
        "",
    ]
    for text in cases:
        with pytest.raises(ValueError):
            canon.loads_strict(text)
        with pytest.raises(ValueError):
            schema.parse_json(text)
    ok = ['{"a": [1, {"b": 2}]}', "[" * 16 + "]" * 16, '{"a": -0}', '{"a": 9007199254740991}', '"é"']
    for text in ok:
        assert schema.parse_json(text) == canon.loads_strict(text)
    assert schema.parse_json(b'{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        schema.parse_json(b'{"a": "\xff"}')


def test_validate_refuses_exactly_what_the_canonical_serialiser_refuses():
    t = ex.TICKET
    for value in (1.5, 2**53, float("nan"), {"é": 1}, "x\ud800"):
        o = mut(t, lambda o, v=value: o["addons"]["estimate"].update(x=v))
        with pytest.raises(SchemaError):
            schema.validate("ticket", o)
        with pytest.raises(ValueError):
            canon.dumps(o)
    deep = mut(t, lambda o: o["addons"]["estimate"].update(x=[[[[[[[[[[[[[[1]]]]]]]]]]]]]]))  # depth 4 + 14 = 18
    bad("ticket", deep, "/addons/estimate/x" + "/0" * 13, "deeper")
    with pytest.raises(ValueError):
        canon.dumps(deep)
    fine = mut(t, lambda o: o["addons"]["estimate"].update(x=[[[[[[[[[[[1]]]]]]]]]]]))  # depth 4 + 11 = 15
    schema.validate("ticket", fine)
    canon.dumps(fine)


def test_text_rules():
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\rb")), "/title", "LF")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="é")), "/title", "NFC")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\x00b")), "/title", "forbidden")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\x7fb")), "/title", "forbidden")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\x85b")), "/title", "forbidden")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a‮b")), "/title", "forbidden")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a⁦b")), "/title", "forbidden")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\nb")), "/title")  # one line
    schema.validate("ticket", mut(ex.TICKET, lambda t: t["acceptance"][0].update(text="two\nlines\twith a tab")))
    schema.validate("ticket", mut(ex.TICKET, lambda t: t.update(title="a​b")))  # other invisibles stay
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\ud800")), "/title", "surrogate")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e["receipt"].update(ms=1.5)), "/receipt/ms", "floats")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e["receipt"].update(ms=1.0)), "/receipt/ms", "floats")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e["receipt"].update(ms=2**53)), "/receipt/ms", "integer")


@pytest.mark.skipif(not schema._UNICODE_16, reason="needs Unicode 16.0 tables (pinned, format doc 11.3)")
def test_unassigned_code_points_are_refused():
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a\U0010fffeb")), "/title", "unassigned")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="a﷐b")), "/title", "unassigned")


def test_string_size_is_counted_in_bytes_and_titles_in_scalar_values():
    t = lambda s: mut(ex.TICKET, lambda o: o.update(title=s))  # noqa: E731
    schema.validate("ticket", t("x" * 200))
    bad("ticket", t("x" * 201), "/title")
    schema.validate("ticket", t("é" * 200))  # 200 scalar values, 400 bytes
    schema.validate("ticket", t("\U0001f600" * 200))  # 200 scalar values, 800 bytes
    bad("ticket", t("\U0001f600" * 201), "/title")
    a = lambda s: mut(ex.TICKET, lambda o: o["acceptance"][0].update(text=s))  # noqa: E731
    schema.validate("ticket", a("x" * 4096))
    bad("ticket", a("x" * 4097), "/acceptance/0/text", "4096 bytes")
    schema.validate("ticket", a("é" * 2048))  # 2048 characters = 4096 bytes
    bad("ticket", a("é" * 2049), "/acceptance/0/text", "4096 bytes")  # 2049 characters = 4098 bytes


def test_ticket_json_size_limit():
    big = mut(ex.TICKET, lambda t: t["acceptance"].extend({"id": f"AC{i}", "text": "x" * 3000} for i in range(4, 100)))
    bad("ticket", big, "", "262144")
    ok = mut(ex.TICKET, lambda t: t["acceptance"].extend({"id": f"AC{i}", "text": "x" * 3000} for i in range(4, 60)))
    schema.validate("ticket", ok)


def test_event_line_limit_and_depth():
    e = ev("ticket.updated")
    e["set"]["ticket.acceptance"] = [{"id": f"AC{i}", "text": "x" * 4000} for i in range(1, 140)]
    e["base_rev"]["ticket.acceptance"] = ex.H
    bad("event", e, "", "524288")


# ---------------------------------------------------------------- workspace and keys.jsonl
def test_workspace_invalid():
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.pop("host")), "", "host")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.pop("settings")), "", "settings")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.update(extra=1)), "", "extra")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["members"][0].update(role="boss")), "/members/0/role")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["members"][0].update(person="p_sev")), "/members/0/person")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["members"].append(ex.fresh(o["members"][0]))), "/members/3/person")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"].pop("code")), "/gates", "code")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(count=0)), "/gates/plan/count")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(count="1")), "/gates/plan/count")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].pop("independent")), "/gates/plan", "independent")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(who="x")), "/gates/plan", "who")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(approvers="owner")), "/gates/plan/approvers")
    bad(
        "workspace",
        mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(approvers=["owner", "viewer"])),
        "/gates/plan/approvers/1",
    )
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(approvers=[])), "/gates/plan/approvers")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(applies="some")), "/gates/plan/applies")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(applies=["story"])), "/gates/plan/applies/0")
    schema.validate("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["plan"].update(applies=["bug", "feature"])))
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["workspace"].update(id="nope")), "/workspace/id")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["workspace"].update(id=ex.WS.upper())), "/workspace/id")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["workspace"].update(prefix="demo")), "/workspace/prefix")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["host"].update(wsk_pub="p256:abc")), "/host/wsk_pub")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["settings"].update(grant_hours=25)), "/settings/grant_hours")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["settings"].update(claim_ttl_min=14)), "/settings/claim_ttl_min")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["settings"].update(lease_ttl_min=4)), "/settings/lease_ttl_min")
    bad(
        "workspace",
        mut(ex.WORKSPACE, lambda o: o["settings"]["repos"].update({"-x": {"path": "p"}})),
        "/settings/repos",
    )
    bad(
        "workspace", mut(ex.WORKSPACE, lambda o: o["addons"]["dashboard"].update(extra=1)), "/addons/dashboard", "extra"
    )
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["addons"].update({"ticket": {"enabled": True}})), "/addons")
    bad("workspace", mut(ex.WORKSPACE, lambda o: o.update(schema="orch.workspace/1")), "/schema")


def test_workspace_code_gate_rules_d59():
    bad("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["code"].update(**{"not": []})), "/gates/code/not")
    bad(
        "workspace",
        mut(ex.WORKSPACE, lambda o: o["gates"]["code"].update(independent=False)),
        "/gates/code/independent",
    )
    schema.validate("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["code"].update(applies="all", count=2)))
    # requirements, plan and verify may stay not independent (the single-owner default)
    schema.validate("workspace", mut(ex.WORKSPACE, lambda o: o["gates"]["verify"].update(independent=True)))


def test_keys_line_invalid():
    bad("keys-line", {"key": "DEMO-0043", "uid": ex.U1}, "", "at")
    bad("keys-line", {"key": "DEMO-0043", "at": ex.KEYS_LINE["at"]}, "", "uid")
    bad("keys-line", {**ex.KEYS_LINE, "x": 1}, "", "x")
    bad("keys-line", {**ex.KEYS_LINE, "key": "demo-1"}, "/key")
    bad("keys-line", {**ex.KEYS_LINE, "at": "2026-02-30T00:00:00Z"}, "/at", "calendar")


KEY_CASES = [
    ("DEMO-0043", True), ("DEMO-12345", True), ("DEMO-0001", True), ("DEMO-9999", True), ("D-0001", True),
    ("DEMO-00043", False), ("DEMO-43", False), ("DEMO-0000", False), ("DEMO-043", False), ("demo-0043", False),
    ("X" * 17 + "-0001", False), ("DEMO-0043 ", False),
]  # fmt: skip


@pytest.mark.parametrize("key,ok", KEY_CASES)
def test_key_form(key, ok):
    o = {**ex.KEYS_LINE, "key": key}
    if ok:
        schema.validate("keys-line", o)
    else:
        bad("keys-line", o, "/key")


# ---------------------------------------------------------------- ticket
def test_ticket_has_17_keys_all_required():
    s = schema.load("ticket")
    assert len(s["properties"]) == 17 and set(s["required"]) == set(s["properties"])
    for k in s["properties"]:
        bad("ticket", mut(ex.TICKET_NEW, lambda t, k=k: t.pop(k)), "", k)


def test_ticket_invalid():
    bad("ticket", mut(ex.TICKET, lambda t: t.update(state="open")), "", "state")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(type="story")), "/type")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(type="investigation")), "/type")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(priority="asap")), "/priority")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(size="xxl")), "/size")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(title="")), "/title")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels="dbt")), "/labels")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels=["Dbt"])), "/labels/0")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels=["a b"])), "/labels/0")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels=["x" * 33])), "/labels/0")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(labels=["dbt", "dbt"])), "/labels")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(uid="short")), "/uid")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(parent="DEMO-40")), "/parent")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(blocked_by=["DEMO-0001", "DEMO-0001"])), "/blocked_by")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"][0].update(id="X1")), "/acceptance/0/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"][0].update(id="AC0")), "/acceptance/0/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].pop("proves")), "/tasks/0", "proves")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].update(status="done")), "/tasks/0", "status")
    bad(
        "ticket", mut(ex.TICKET, lambda t: t["tasks"][0].update(assignee=None)), "/tasks/0/assignee"
    )  # absent, never null
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].update(verify={"cmd": ""})), "/tasks/0/verify/cmd")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][0].update(verify={})), "/tasks/0/verify")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(blocking="yes")), "/questions/0/blocking")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(to="owner")), "/questions/0/to")
    schema.validate("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(to="ticket_owner")))
    bad(
        "ticket",
        mut(ex.TICKET, lambda t: t["questions"][0]["options"][0].update(key="Csv")),
        "/questions/0/options/0/key",
    )
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0]["options"][0].update(extra=1)), "/questions/0/options/0")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"].update(extra=[])), "/links", "extra")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"].pop("prs")), "/links", "prs")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"].update(external=["http://x.example"])), "/links/external/0")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"]["prs"][0].update(url="http://github.com/x")), "/links/prs/0/url")
    bad("ticket", mut(ex.TICKET, lambda t: t["links"].update(repos=["-bad"])), "/links/repos/0")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(visibility="secret")), "/visibility")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(visibility={"restricted": []})), "/visibility/restricted")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(due="2026-11-01T00:00:00Z")), "/due")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(due="tomorrow")), "/due")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(due="2026-02-30")), "/due", "calendar")
    bad("ticket", mut(ex.TICKET, lambda t: t["addons"].update({"Bad Name": {}})), "/addons")
    bad("ticket", mut(ex.TICKET, lambda t: t["addons"].update({"log": {}})), "/addons")  # reserved event prefix
    bad("ticket", mut(ex.TICKET, lambda t: t["addons"].update(estimate=5)), "/addons/estimate")


def test_ticket_valid_variants():
    t = mut(ex.TICKET, lambda t: t.update(visibility={"restricted": ["p_" + ex.PID]}, due="2026-11-01", size=None))
    t["addons"]["estimate"]["anything"] = {"nested": [1, "x"]}
    schema.validate("ticket", t)
    schema.validate("ticket", mut(ex.TICKET, lambda t: t["links"].update(branches={"a": "feat/x_y-1"})))


def test_ticket_cross_field():
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"][1].update(proves=["AC9"])), "/tasks/1/proves/0", "AC9")
    bad("ticket", mut(ex.TICKET, lambda t: t["acceptance"].append({"id": "AC1", "text": "dup"})), "/acceptance/3/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["tasks"].append({**t["tasks"][0]})), "/tasks/4/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"].append({**t["questions"][0]})), "/questions/1/id")
    bad("ticket", mut(ex.TICKET, lambda t: t["questions"][0].update(recommended="zzz")), "/questions/0/recommended")
    bad(
        "ticket",
        mut(ex.TICKET, lambda t: t["questions"][0]["options"].append({"key": "csv", "label": "x"})),
        "/questions/0/options",
    )
    bad("ticket", mut(ex.TICKET, lambda t: t.update(parent="DEMO-0043")), "/key")
    bad("ticket", mut(ex.TICKET, lambda t: t.update(blocked_by=["DEMO-0043"])), "/key")


@pytest.mark.parametrize("branch", ["a..b", "a b", "a~1", "a^", "a:b", "a?", "a*", "a[", "a\\b", "/a", "a/", "a//b",
                                    ".a", "a/.b", "a.lock", "a/b.lock", "a.", "a@{b"])  # fmt: skip
def test_branch_names_follow_git_ref_rules(branch):
    t = mut(ex.TICKET, lambda t: t["links"]["branches"].update({"acme-energy-dbt": branch}))
    with pytest.raises(SchemaError) as e:
        schema.validate("ticket", t)
    assert e.value.path.startswith("/links/branches"), e.value


# ---------------------------------------------------------------- body
def test_body_sections_by_type():
    schema.validate("body", {"type": "feature", "sections": {}})  # a draft may be empty
    schema.validate("body", {"type": "feature", "sections": {"plan": ""}})  # an empty section is written
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(findings="x")), "/sections/findings", "no section")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b.update(type="story")), "/type")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b.update(type="investigation")), "/type")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan=3)), "/sections/plan")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(Plan="x")), "/sections")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="x" * 70_000)), "/sections/plan", "65536")
    schema.validate("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="x" * 65_536)))
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="é" * 32_769)), "/sections/plan", "65536")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="\nx")), "/sections/plan", "LF")
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="x\n")), "/sections/plan", "LF")
    schema.validate("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update(plan="x \n\n y  \t")))
    schema.validate(
        "body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update({"estimate.notes": "x", "summary": "s"}))
    )
    bad("body", mut(ex.BODY_FEATURE, lambda b: b["sections"].update({"Estimate.notes": "x"})), "/sections")


@pytest.mark.parametrize(
    "t,absent",
    [
        ("feature", set()),
        ("bug", set()),
        ("chore", {"out_of_scope", "verification", "findings"}),
        ("spike", {"out_of_scope", "verification"}),
        ("epic", {"plan", "verification", "findings"}),
    ],
)
def test_body_table_row_by_row(t, absent):
    every = set(schema.load("common")["$defs"]["sectionId"]["enum"])
    for sid in every:
        b = {"type": t, "sections": {sid: "x"}}
        if sid in absent or (sid == "findings" and t != "spike"):
            bad("body", b, "/sections/" + sid, "no section")
        else:
            schema.validate("body", b)


# ---------------------------------------------------------------- envelope
ENVELOPE_REQUIRED = ["v", "id", "seq", "at", "type", "actor", "based_on", "prev", "hash_v", "host_sig"]


def test_envelope_invalid():
    g = "gate.approved"
    for k in (*ENVELOPE_REQUIRED, "sig", "auth", "roster_v"):
        bad("event", ev(g, **{k: None}), "", k, log="ticket")
    bad("event", ev(g, v=1), "/v")
    bad("event", ev(g, hash_v=2), "/hash_v")
    bad("event", ev(g, seq=0), "/seq")
    bad("event", ev(g, seq="5"), "/seq")
    bad("event", ev(g, at="2026-10-09 09:10:11"), "/at")
    bad("event", ev(g, at="2026-10-09T09:10:11.5Z"), "/at")
    bad("event", ev(g, at="2026-10-09T09:10:60Z"), "/at")  # seconds are 00-59
    bad("event", ev(g, at="2026-02-30T09:10:11Z"), "/at", "calendar")
    bad("event", ev(g, prev="sha256:zz"), "/prev")
    bad("event", ev(g, prev=ex.H2.upper()), "/prev")
    bad("event", ev(g, prev=ex.H2 + "\n"), "/prev")
    bad("event", ev(g, host_sig="p256:abc"), "/host_sig")
    bad("event", ev(g, host_sig=ex.SIG[:-1]), "/host_sig")  # wrong length
    bad("event", ev(g, host_sig=ex.SIG + "="), "/host_sig")  # padding
    bad("event", ev(g, host_sig="A" * 85 + "B"), "/host_sig")  # non-zero trailing bits
    bad("event", ev(g, extra=1), "", "extra")
    bad("event", ev(g, id="01J9ZP"), "/id")
    bad("event", ev(g, id=ex.EID.lower()), "/id")
    bad("event", ev(g, ws_seq=0), "/ws_seq")
    bad("event", ev(g, roster_v=-1), "/roster_v")
    bad("event", ev(g, auth="presence"), "/auth")
    bad("event", ev(g, auth="touchid"), "/auth")
    bad("event", ev(g, sig="p256:" + ex.SIG), "/sig")
    for a in ("passphrase", "secure-enclave", "secure-enclave-unlocked", "webauthn", "tpm", "windows-hello"):
        schema.validate("event", ev(g, auth=a), log="ticket")
    bad("event", ev(g, list_seq=7), "", "list_seq")  # F1 names it roster_v


def test_envelope_seq_one_and_chain_links():
    created = ex.EVENTS["ticket.created"]
    assert created["seq"] == 1 and created["prev"] is None and created["based_on"] is None
    bad("event", mut(created, lambda e: e.update(prev=ex.H2)), "/prev")
    bad("event", mut(created, lambda e: e.update(based_on=ex.H2)), "/based_on")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e.update(prev=None)), "/prev")
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e.update(based_on=None)), "/based_on")
    bad("event", mut(created, lambda e: e.update(seq=2)), "/seq")  # ticket.created is always seq 1
    bad("event", mut(ex.EVENTS["workspace.created"], lambda e: e.update(seq=2, prev=ex.H2, based_on=ex.H2)), "/seq")
    bad("event", mut(ex.EVENTS["workspace.created"], lambda e: e.update(roster_v=1)), "/roster_v")


def test_actor_shapes_and_unattended_rules():
    t = "task.done"
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].pop("grant")), "/actor")  # neither grant nor unattended
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].update(unattended=True)), "/actor")  # both
    bad("event", mut(ex.EVENTS["log.added"], lambda e: e["actor"].update(**{"for": "p_" + ex.PID})), "/actor")
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].update(id="Claude")), "/actor")
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].update(session="s_" + ex.U1 + ".0")), "/actor")
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].update(session="s_" + ex.U1 + ".1.2.3.4")), "/actor")
    schema.validate(
        "event", mut(ex.EVENTS[t], lambda e: e["actor"].update(session="s_" + ex.U1 + ".1.2.3")), log="ticket"
    )
    bad("event", mut(ex.EVENTS[t], lambda e: e["actor"].update(grant="gr_x")), "/actor/grant")
    bad("event", mut(ex.EVENTS["gate.approved"], lambda e: e["actor"].update(device=None)), "/actor/device")
    bad("event", mut(ex.EVENTS["gate.approved"], lambda e: e["actor"].update(id="p_sev")), "/actor/id")
    bad("event", mut(ex.EVENTS["gate.invalidated"], lambda e: e["actor"].update(id="x")), "/actor")  # host: kind only
    bad("event", mut(ex.EVENTS[t], lambda e: e.update(sig=ex.SIG)), "", "sig")  # agents never sign
    bad("event", mut(ex.EVENTS[t], lambda e: e.update(auth="passphrase")), "", "auth")
    bad("event", mut(ex.EVENTS[t], lambda e: e.update(roster_v=1)), "", "roster_v")
    bad("event", mut(ex.EVENTS["gate.invalidated"], lambda e: e.update(sig=ex.SIG)), "", "sig")  # host: host_sig only
    schema.validate("event", ex.EVENTS["log.added"], log="ticket")


def test_addon_actor_is_refused_in_p1():
    addon = {"kind": "addon", "id": "estimate", "grant": "gr_" + ex.U1}
    bad("event", mut(ex.EVENTS["ticket.updated"], lambda e: e.update(actor=addon)), "/actor/kind")
    bad("event", mut(ex.EVENTS["artifact.added"], lambda e: e.update(actor=addon)), "/actor/kind")


def test_custom_addon_events_and_reserved_prefixes_are_refused():
    bad("event", ex.ADDON_EVENT, "/type", "custom addon events are refused")
    for t in (
        "gate.bogus",
        "ticket.moved",
        "invalid.thing",
        "device.lost",
        "grant.bogus",
        "nodot",
        "log",
        "session.granted",
    ):
        bad("event", mut(ex.ADDON_EVENT, lambda e, t=t: e.update(type=t)), "/type", "unknown event type")
    bad("event", mut(ex.ADDON_EVENT, lambda e: e.pop("host_sig")), "", "host_sig")
    bad("event", mut(ex.ADDON_EVENT, lambda e: e.update(type="Bad Type")), "/type")
    for p in schema.load("common")["$defs"]["reservedPrefix"]["enum"]:
        bad("addon-manifest", mut(ex.MANIFEST, lambda m, p=p: m.update(name=p)), "/name")
    schema.validate("addon-manifest", mut(ex.MANIFEST, lambda m: m.update(name="logs")))
    bad("event", ex.EVENTS["addon.granted"] | {"name": "gate"} | {"id": ex.EID}, "/name", log="workspace")


# ---------------------------------------------------------------- who may append what (5.4 actor column)
def _doc_rows(section_start, section_end):
    text = DOC.read_text()
    block = text[text.index(section_start) : text.index(section_end)]
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        m = re.fullmatch(r"`([a-z_.]+)`", cells[0]) if len(cells) == 4 else None
        if m:
            rows[m.group(1)] = cells[1:]
    return rows


TICKET_ROWS = _doc_rows("#### 5.4.1 Ticket log", "#### 5.4.2 Workspace log")
WS_ROWS = _doc_rows("#### 5.4.2 Workspace log", "The **member-list version**")


def _allowed_actors(actor_cell, notes):
    kinds = set()
    for token in re.sub(r"\(.*?\)", "", actor_cell).split(","):
        token = token.strip()
        if token == "P":
            kinds |= {"person"}
        elif token == "A":
            kinds |= {"person", "agent"}
        elif token == "U":
            kinds |= {"unattended"}
        elif token == "H":
            kinds |= {"host"}
        else:
            assert token == "D", token  # addon actors are refused in P1
    if "Agents only" in notes:
        kinds = {"agent"}
    return kinds


def _with_actor(t, kind, base=None):
    e = ex.fresh(base or ex.EVENTS[t])
    actor = ex.fresh(ex.ACTORS[kind])
    e["actor"] = actor
    for k in ("sig", "auth", "roster_v"):
        e.pop(k, None)
    if kind == "person":
        e.update(sig=ex.SIG, auth="passphrase", roster_v=0 if t == "workspace.created" else 7)
    if t == "ticket.created":
        e["owner"] = actor["id"] if kind == "person" else actor.get("for", "p_" + ex.PID)
    if t == "claim.released":
        e["session"] = actor.get("session", e["session"])
        e["reason"] = {"person": "released", "host": "expired"}.get(kind, "handoff")
    if t == "artifact.added" and kind == "unattended":
        e.pop("ac"), e.pop("task")
    return e


@pytest.mark.parametrize("t", sorted(ex.WORKSPACE_ONLY | set(cov_t for cov_t in TICKET_ROWS)))
def test_actor_column_matches_the_doc(t):
    row = TICKET_ROWS.get(t) or WS_ROWS[t]
    allowed = _allowed_actors(row[0], row[2])
    for kind in ("person", "agent", "unattended", "host"):
        e = _with_actor(t, kind)
        log = log_of(t)
        if kind in allowed:
            schema.validate("event", e, log=log)
        else:
            with pytest.raises(SchemaError) as err:
                schema.validate("event", e, log=log)
            assert err.value.path.startswith("/actor"), (t, kind, err.value)


def test_person_events_need_signature_auth_and_roster_version():
    for t, e in ex.EVENTS.items():
        e = ex.EVENTS[t]
        if e["actor"]["kind"] != "person":
            continue
        for k in ("sig", "auth", "roster_v"):
            bad("event", mut(e, lambda o, k=k: o.pop(k)), "", k, log=log_of(e["type"]))


def test_agent_and_host_never_stand_in_for_a_person():
    for t in ("gate.approved", "verdict.given", "question.answered", "ticket.closed", "restore", "grant.issued",
              "member.added", "invalid.acknowledged", "addon.granted", "settings.changed"):  # fmt: skip
        e = ex.EVENTS[t]
        log = log_of(t)
        for kind in ("agent", "unattended", "host"):
            with pytest.raises(SchemaError) as err:
                schema.validate("event", _with_actor(t, kind), log=log)
            assert err.value.path.startswith("/actor"), (t, kind)
        assert e["actor"]["kind"] == "person"


def test_host_only_events_refuse_everyone_else():
    for t in ("gate.invalidated", "branch.pushed", "edit.external", "projection.repaired"):
        for kind in ("person", "agent", "unattended"):
            with pytest.raises(SchemaError) as err:
                schema.validate("event", _with_actor(t, kind), log="ticket")
            assert err.value.path == "/actor/kind", (t, kind, err.value)


def test_unattended_is_only_for_three_types():
    ok = {t for t in ex.EVENTS if t in ("log.added", "question.asked", "artifact.added")}
    for key, e in ex.EVENTS.items():
        t = e["type"]
        if t in ex.WORKSPACE_ONLY or key == "gate.approved.code":
            continue
        u = _with_actor(t, "unattended")
        if t in ok:
            schema.validate("event", u, log=log_of(t))
        else:
            with pytest.raises(SchemaError):
                schema.validate("event", u, log=log_of(t))


def test_unattended_artifact_rules():
    u = _with_actor("artifact.added", "unattended")
    schema.validate("event", u, log="ticket")
    bad("event", mut(u, lambda e: e.update(ac="AC1")), "")
    bad("event", mut(u, lambda e: e.update(task="T2")), "")
    bad("event", mut(u, lambda e: e.update(kind="feedback")), "/actor/kind")
    bad("event", _with_actor("artifact.replaced", "unattended"), "/actor", log="ticket")


# ---------------------------------------------------------------- log rules
def test_log_membership_and_ws_seq():
    for t in sorted(ex.WORKSPACE_ONLY):
        bad("event", ex.EVENTS[t], "/type", "ticket log", log="ticket")
    for t in sorted({e["type"] for e in ex.EVENTS.values()} - ex.WORKSPACE_ONLY - set(schema._BOTH_LOGS)):
        bad("event", mut(ex.EVENTS[t], lambda e: e.pop("ws_seq", None)), "/type", "workspace log", log="workspace")
    bad("event", ev("task.done", ws_seq=None), "", "ws_seq", log="ticket")
    bad("event", ev("member.removed", ws_seq=3), "/ws_seq", log="workspace")
    with pytest.raises(ValueError):
        schema.validate("event", ex.EVENTS["task.done"], log="nope")
    # seq 1 of a ticket log is ticket.created and nothing else; of the workspace log workspace.created
    bad("event", mut(ex.EVENTS["task.done"], lambda e: e.update(seq=1, prev=None, based_on=None)), "/seq", log="ticket")
    bad(
        "event",
        mut(ex.EVENTS["member.removed"], lambda e: e.update(seq=1, prev=None, based_on=None)),
        "/seq",
        log="workspace",
    )
    schema.validate("event", ex.EVENTS["ticket.created"], log="ticket")
    schema.validate("event", ex.EVENTS["workspace.created"], log="workspace")
    bad("event", mut(ex.EVENTS["ticket.created"], lambda e: e.pop("ws_seq")), "", "ws_seq", log="ticket")


# ---------------------------------------------------------------- payloads, type by type
def test_exact_field_sets():
    """Every type refuses an extra field and a missing required one."""
    for key, e in ex.EVENTS.items():
        t = e["type"]
        log = log_of(t)
        bad("event", mut(e, lambda o: o.update(zzz=1)), "", "zzz", log=log)
        required = set(schema.load("event." + t)["required"]) - {"type"}
        for f in sorted(required):
            if key == "gate.approved.code":
                continue
            bad("event", mut(e, lambda o, f=f: o.pop(f)), "", f, log=log)


def test_ticket_created_and_updated():
    E = ex.EVENTS
    bad("event", mut(E["ticket.created"], lambda e: e.pop("ticket_type")), "", "ticket_type")
    bad("event", mut(E["ticket.created"], lambda e: e.update(ticket_type="story")), "/ticket_type")
    bad("event", mut(E["ticket.created"], lambda e: e.update(title="x" * 201)), "/title")
    bad("event", mut(E["ticket.created"], lambda e: e.update(key="DEMO-43")), "/key")
    bad("event", mut(E["ticket.created"], lambda e: e.update(owner="p_" + ex.PID2)), "/owner", "for")
    schema.validate(
        "event",
        mut(
            E["ticket.created"],
            lambda e: e.update(actor=ex.PERSON, owner="p_" + ex.PID, sig=ex.SIG, auth="passphrase", roster_v=7),
        ),
        log="ticket",
    )
    bad("event", mut(E["ticket.created"], lambda e: e.update(owner="bob")), "/owner")
    U = "ticket.updated"

    def upd(set_=None, sections=None, base_rev=None):
        e = ex.fresh(ex.EVENTS[U])
        e.pop("set"), e.pop("sections")
        if set_ is not None:
            e["set"] = set_
        if sections is not None:
            e["sections"] = sections
        e["base_rev"] = (
            base_rev
            if base_rev is not None
            else {
                **{p: ex.H for p in (set_ or {})},
                **{"body." + s: ex.H for s in (sections or {})},
            }
        )
        return e

    schema.validate("event", upd({"ticket.title": "x"}), log="ticket")
    schema.validate("event", upd(sections={"plan": None}), log="ticket")  # a removed section
    bad("event", upd(), "", "")  # at least one of set, sections
    bad("event", upd({"ticket.title": "x"}, base_rev={}), "/base_rev")
    bad("event", upd({"ticket.title": "x"}, base_rev={"ticket.title": ex.H, "body.plan": ex.H}), "/base_rev", "exactly")
    bad(
        "event", upd({"ticket.title": "x", "ticket.size": "s"}, base_rev={"ticket.title": ex.H}), "/base_rev", "exactly"
    )
    for protected in (
        "ticket.schema",
        "ticket.uid",
        "ticket.key",
        "ticket.visibility",
        "ticket.questions",
        "ticket.addons",
        "ticket.addons.estimate",
        "ticket.nope",
        "body.plan",
    ):
        with pytest.raises(SchemaError) as err:
            schema.validate("event", upd({protected: "x"}), log="ticket")
        assert err.value.path in ("/set", "/base_rev"), (protected, err.value)
    schema.validate("event", upd({"ticket.addons.estimate.points": 5}), log="ticket")
    bad("event", upd({"ticket.addons.estimate.points": 5}) | {"zzz": 1}, "", "zzz")
    bad("event", upd({"ticket.addons.Estimate.points": 5}), "/set")
    bad("event", upd({"ticket.title": ""}), "/set/ticket.title")  # values are checked against ticket.json
    bad("event", upd({"ticket.priority": "asap"}), "/set/ticket.priority")
    bad("event", upd({"ticket.size": "xxl"}), "/set/ticket.size")
    schema.validate("event", upd({"ticket.size": None}), log="ticket")
    bad("event", upd({"ticket.due": "2026-02-30"}), "/set/ticket.due", "calendar")
    bad(
        "event",
        upd({"ticket.links": ex.TICKET["links"] | {"branches": {"a": "a..b"}}}),
        "/set/ticket.links/branches/a",
        "ref name",
    )
    bad(
        "event",
        upd({"ticket.tasks": [{"id": "T1", "text": "a", "verify": None, "proves": []}] * 2}),
        "/set/ticket.tasks",
        "duplicate",
    )
    bad("event", upd(sections={"plan": {"hash": ex.H, "refs": ["b.png", "a.png"]}}), "/sections/plan/refs", "sorted")
    bad("event", upd(sections={"plan": {"hash": ex.H, "refs": ["a.png", "a.png"]}}), "/sections/plan/refs")
    bad("event", upd(sections={"plan": {"hash": ex.H}}), "/sections/plan")
    bad("event", upd(sections={"nope": None}), "/sections")
    bad("event", upd(sections={}), "/sections")


def test_status_close_reopen_visibility_people_policy():
    E = ex.EVENTS
    bad("event", ev("status.changed", to="done"), "/to")
    bad("event", ev("status.changed", **{"from": "stuck"}), "/from")
    schema.validate(
        "event", ev("status.changed", **{"from": "in_progress"}), log="ticket"
    )  # the status rule refuses it
    bad("event", ev("ticket.closed", resolution="done"), "/resolution")
    bad("event", ev("ticket.closed", resolution="obsolete"), "/resolution")  # duplicate_of only with duplicate
    schema.validate("event", ev("ticket.closed", resolution="wont_do", duplicate_of=None), log="ticket")
    bad("event", ev("ticket.closed", duplicate_of="DEMO-40"), "/duplicate_of")
    bad("event", ev("visibility.changed", visibility="secret"), "/visibility")
    bad("event", ev("people.changed", role="viewers"), "/role")
    bad("event", ev("people.changed", role="ticket_owner"), "/role")
    bad("event", ev("people.changed", role="owner", add=[]), "/add")
    bad("event", ev("people.changed", role="owner", add=["p_" + ex.PID, "p_" + ex.PID2]), "/add")
    bad("event", ev("people.changed", add=None), "", "add")
    bad("event", ev("people.changed", add=["p_x"]), "/add/0")
    bad("event", ev("policy.changed", gates={}), "/gates")
    bad("event", ev("policy.changed", gates={"deploy": ex.policy()}), "/gates", "deploy")
    bad("event", ev("policy.changed", gates={"plan": {**ex.policy(), "count": 0}}), "/gates/plan/count")
    bad("event", ev("policy.changed", gates={"plan": {"approvers": ["owner"], "count": 1}}), "/gates/plan", "not")
    bad("event", ev("policy.changed", gates={"code": ex.policy(("owner",), 1, (), "off", True)}), "/gates/code/not")
    bad(
        "event",
        ev("policy.changed", gates={"code": ex.policy(("owner",), 1, ("assignees",), "off", False)}),
        "/gates/code/independent",
    )
    assert E["people.changed"]["role"] == "reviewers"


def test_claims_and_tasks():
    E = ex.EVENTS
    bad("event", mut(E["claim.taken"], lambda e: e["takeover"].pop("reason")), "/takeover", "reason")
    bad("event", mut(E["claim.taken"], lambda e: e.update(expires_at="2026-10-09T18:00:00Z")), "", "expires_at")
    bad("event", mut(E["claim.taken"], lambda e: e["takeover"].update(from_session="x")), "/takeover/from_session")
    schema.validate("event", mut(E["claim.taken"], lambda e: e.pop("takeover")), log="ticket")
    bad("event", _with_actor("claim.taken", "person"), "/actor/kind", log="ticket")  # agents only
    bad("event", _with_actor("task.started", "person"), "/actor/kind", log="ticket")
    bad("event", ev("claim.released", reason="x"), "/reason")
    bad("event", ev("claim.released", reason="expired"), "/reason", "released or handoff")  # an agent can't expire
    bad("event", ev("claim.released", session="s_" + ex.EID), "/session", "own claim")
    bad("event", _with_actor("claim.released", "host") | {"reason": "released"}, "/reason", "host", log="ticket")
    bad("event", _with_actor("claim.released", "person") | {"reason": "expired"}, "/reason", "person", log="ticket")
    for r in ("expired", "grant_ended", "member_removed", "ticket_done", "ticket_closed"):
        schema.validate("event", _with_actor("claim.released", "host") | {"reason": r}, log="ticket")
    T = "task.done"
    bad("event", ev(T, task="3"), "/task")
    bad("event", ev(T, task="T0"), "/task")
    bad("event", mut(E[T], lambda e: e["receipt"].pop("exit")), "/receipt", "exit")
    bad("event", mut(E[T], lambda e: e["receipt"].pop("repo")), "/receipt", "repo")
    bad("event", mut(E[T], lambda e: e["receipt"].update(extra=1)), "/receipt", "extra")
    bad("event", mut(E[T], lambda e: e["receipt"].update(commit="b7e1f02")), "/receipt/commit")  # never abbreviated
    bad("event", mut(E[T], lambda e: e["receipt"].update(commit="B7E1F02C" * 5)), "/receipt/commit")
    bad("event", mut(E[T], lambda e: e["receipt"].update(ms=-1)), "/receipt/ms")
    bad("event", mut(E[T], lambda e: e.update(log="../x")), "/log")
    schema.validate("event", mut(E[T], lambda e: e["receipt"].update(repo=None, commit=None)), log="ticket")
    schema.validate("event", mut(E[T], lambda e: e["receipt"].update(commit="a" * 64)), log="ticket")  # SHA-256 repos
    schema.validate("event", mut(E[T], lambda e: [e.pop("receipt"), e.pop("log"), e.pop("text")]), log="ticket")
    for t in ("task.skipped", "task.blocked"):
        bad("event", mut(E[t], lambda e: e.pop("reason")), "", "reason")
    schema.validate("event", mut(E["task.reopened"], lambda e: e.update(reason="again")), log="ticket")


def test_handoff_log_and_questions():
    E = ex.EVENTS
    schema.validate("event", ev("handoff.written", text="x" * 2048), log="ticket")
    bad("event", ev("handoff.written", text="x" * 2049), "/text", "2048")
    bad("event", ev("handoff.written", text="é" * 1025), "/text", "2048")
    schema.validate("event", ev("log.added", text="x" * 4096), log="ticket")
    bad("event", ev("log.added", text="x" * 4097), "/text", "4096")
    bad("event", ev("log.added", text=""), "/text")
    Q = "question.asked"
    bad("event", mut(E[Q], lambda e: e.update(question="Q1")), "/question")  # the full question, not just the id
    bad("event", mut(E[Q], lambda e: e["question"].pop("blocking")), "/question", "blocking")
    bad("event", mut(E[Q], lambda e: e["question"].update(recommended="zzz")), "/question/recommended")
    bad("event", mut(E[Q], lambda e: e["question"].update(to="owner")), "/question/to")
    bad("event", mut(E[Q], lambda e: e.update(qid=ex.hex32("x").upper())), "/qid")
    bad("event", mut(E[Q], lambda e: e.update(qid=ex.hex32("x")[:-1])), "/qid")
    bad("event", mut(E[Q], lambda e: e.update(hash="x")), "/hash")
    A = "question.answered"
    bad("event", ev(A, option=None, text=None), "")  # at least one of option, text
    schema.validate("event", ev(A, option=None), log="ticket")
    schema.validate("event", ev(A, text=None), log="ticket")
    bad("event", ev(A, evidence={"decision_id": "x"}), "", "evidence")  # refused in P1
    bad("event", ev(A, question="Q0"), "/question")
    bad("event", ev(A, answer="csv"), "", "answer")  # the 8 Oct draft's name


def test_gates_and_verdicts():
    E = ex.EVENTS
    G = "gate.approved"
    bad("event", ev(G, gate="verify"), "/gate")  # a verify decision is a verdict
    bad("event", ev(G, gate="Plan"), "/gate")
    bad("event", ev(G, hash="abc"), "/hash")
    bad("event", ev(G, gate_gen=-1), "/gate_gen")
    bad("event", ev(G, gate_gen="2"), "/gate_gen")
    bad("event", ev(G, policy_hash=None), "", "policy_hash")
    bad("event", ev(G, source_sha=ex.SOURCE), "", "source_sha")  # only on code
    c = E["gate.approved.code"]
    schema.validate("event", c, log="ticket")
    bad("event", mut(c, lambda e: e.pop("source_sha")), "", "source_sha")  # always on code
    bad("event", mut(c, lambda e: e["source_sha"][0].update(sha="abc")), "/source_sha/0/sha")
    bad("event", mut(c, lambda e: e["source_sha"][0].update(ref="refs/tags/x")), "/source_sha/0/ref")
    bad("event", mut(c, lambda e: e["source_sha"][0].update(ref="refs/heads/a..b")), "/source_sha/0/ref", "ref name")
    bad("event", mut(c, lambda e: e["source_sha"][0].update(repo="http://x")), "/source_sha/0/repo")
    schema.validate("event", mut(c, lambda e: e["source_sha"][0].update(repo="local:acme")), log="ticket")
    two = {"repo": "https://a.example/z", "ref": "refs/heads/m", "sha": "c" * 40}
    bad("event", mut(c, lambda e: e["source_sha"].append(two)), "/source_sha", "sorted")
    schema.validate("event", mut(c, lambda e: e["source_sha"].insert(0, two)), log="ticket")
    bad("event", mut(c, lambda e: e["source_sha"].append(ex.fresh(e["source_sha"][0]))), "/source_sha")
    bad("event", ev("gate.changes_requested", text=None), "", "text")
    bad("event", ev("gate.changes_requested", gate="verify"), "/gate")
    V = "verdict.given"
    bad("event", ev(V, outcome="maybe"), "/outcome")
    bad("event", ev(V, outcome="accepted"), "/outcome")
    bad("event", ev(V, source_sha=None), "", "source_sha")
    bad("event", ev(V, outcome="fail", text=None), "", "text")
    schema.validate("event", ev(V, outcome="pass", text=None), log="ticket")
    schema.validate("event", ev(V, source_sha=[]), log="ticket")
    bad("event", ev(V, gate="verify"), "", "gate")  # the verdict is verify's: no gate field


def test_host_events():
    E = ex.EVENTS
    bad("event", mut(E["gate.invalidated"], lambda e: e.update(cause="new_commit")), "/cause")
    for c in (
        "new_commits",
        "content_changed",
        "policy_changed",
        "member_changed",
        "device_compromised",
        "conflict_resolved",
    ):
        schema.validate("event", ev("gate.invalidated", cause=c), log="ticket")
    bad("event", ev("gate.invalidated", gate="deploy"), "/gate")
    bad("event", ev("gate.invalidated", voided=[ex.EID, ex.EID]), "/voided")
    bad("event", ev("gate.invalidated", voided=["x"]), "/voided/0")
    B = "branch.pushed"
    bad("event", mut(E[B], lambda e: e.pop("before")), "", "before")
    bad("event", mut(E[B], lambda e: e.pop("repo_name")), "", "repo_name")
    bad("event", mut(E[B], lambda e: e.update(sha="abc")), "/sha")
    bad("event", mut(E[B], lambda e: e.update(repo_id="git@github.com:a/b")), "/repo_id")
    bad("event", mut(E[B], lambda e: e.update(ref="main")), "/ref")
    bad("event", mut(E[B], lambda e: e.update(ref="refs/heads/a b")), "/ref")
    bad("event", mut(E[B], lambda e: e.update(ref="refs/heads/a..b")), "/ref", "ref name")
    schema.validate("event", mut(E[B], lambda e: e.update(repo_id="local:acme")), log="ticket")
    before = {"repo_id": "https://github.com/acme/energy-dbt", "ref": "refs/heads/x", "sha": "a" * 40}
    schema.validate("event", mut(E[B], lambda e: e.update(before=before)), log="ticket")
    bad("event", mut(E[B], lambda e: e.update(before={**before, "extra": 1})), "/before", "extra")
    bad("event", mut(E[B], lambda e: e.update(before={**before, "ref": "x"})), "/before/ref")
    X = "edit.external"
    bad("event", mut(E[X], lambda e: e.update(files=["x.txt"])), "", "files")
    bad("event", mut(E[X], lambda e: e.update(sections={})), "/sections")
    bad("event", mut(E[X], lambda e: e.update(normalised="no")), "/normalised")
    bad("event", mut(E[X], lambda e: e.update(voided_gates=["plan", "plan"])), "/voided_gates")
    bad("event", mut(E[X], lambda e: e["sections"]["plan"].update(refs=["b.png", "a.png"])), "/sections/plan/refs")
    bad("event", mut(E[X], lambda e: e.update(sections={"plan": {"hash": ex.H}})), "/sections/plan")
    P = "projection.repaired"
    bad("event", mut(E[P], lambda e: e.update(cause="other")), "/cause")
    for c in ("external_edit", "projection_mismatch", "keys_mismatch"):
        schema.validate("event", mut(E[P], lambda e, c=c: e.update(cause=c)), log="ticket")
    schema.validate("event", mut(E[P], lambda e: e.pop("fields")), log="ticket")
    bad("event", mut(E[P], lambda e: e.update(fields="a")), "/fields")


def test_artifact_events():
    A = ex.EVENTS["artifact.added"]
    bad("event", mut(A, lambda e: e.pop("sha256")), "")
    bad("event", mut(A, lambda e: e.pop("bytes")), "")
    bad("event", mut(A, lambda e: e.update(kind="video")), "/kind")  # a file artifact has a core kind
    bad("event", mut(A, lambda e: e.update(bytes="1")), "/bytes")
    bad("event", mut(A, lambda e: e.update(bytes=-1)), "/bytes")
    bad("event", mut(A, lambda e: e.update(name="../x")), "/name")
    bad("event", mut(A, lambda e: e.update(name="a" * 129)), "/name")
    schema.validate("event", mut(A, lambda e: e.update(name="a" * 128)), log="ticket")
    bad("event", mut(A, lambda e: e.update(extra=1)), "", "extra")
    bad("event", mut(A, lambda e: e.update(ac="X")), "/ac")
    bad("event", mut(A, lambda e: e.update(sha256="3f9a" * 16)), "/sha256")  # a digest is sha256:<hex>
    bad("event", mut(A, lambda e: e.update(sha256=ex.digest("x").upper())), "/sha256")
    bad("event", mut(A, lambda e: e.update(label="a\nb")), "/label")
    # an addon artifact has addon + ref instead of a file
    addon = mut(
        A, lambda e: [e.pop(k) for k in ("sha256", "bytes")] and e.update(kind="board", addon="dashboard", ref="b/1")
    )
    schema.validate("event", addon, log="ticket")
    bad("event", mut(addon, lambda e: e.update(sha256=ex.digest("x"))), "")
    bad("event", mut(addon, lambda e: e.pop("ref")), "")
    bad("event", mut(addon, lambda e: e.update(addon="gate")), "/addon")
    bad("event", mut(A, lambda e: e.update(addon="dashboard", ref="x")), "")  # file and addon at once
    # feedback may only be added by a person
    fb = mut(A, lambda e: e.update(kind="feedback"))
    bad("event", fb, "/actor/kind", log="ticket")
    schema.validate("event", _with_actor("artifact.added", "person", fb), log="ticket")
    R = ex.EVENTS["artifact.replaced"]
    bad("event", mut(R, lambda e: e.pop("replaces")), "")
    bad("event", mut(R, lambda e: e.update(replaces=ex.digest("x")[:-1])), "/replaces")
    bad("event", mut(R, lambda e: e.update(addon="dashboard", ref="x")), "")  # files only (the old digest)
    bad("event", mut(R, lambda e: e.update(kind="feedback")), "/actor/kind", log="ticket")


def test_artifact_entry():
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.pop("actor")), "", "actor")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(kind="movie")), "/kind")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(extra=1)), "", "extra")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(sha256="ab" * 32)), "/sha256")
    bad("artifact", mut(ex.ARTIFACT, lambda a: a.update(kind="feedback")), "/actor/kind")
    schema.validate("artifact", mut(ex.ARTIFACT, lambda a: a.update(kind="feedback", actor=ex.PERSON)))
    bad("artifact", mut(ex.ARTIFACT_ADDON, lambda a: a.pop("ref")), "")


def test_restore_and_invalid_acknowledged():
    R = "restore"
    bad("event", ev(R, from_seq=0), "/from_seq")
    bad("event", ev(R, from_seq=5), "/seq", "from_seq + 1")
    bad("event", ev(R, head=ex.H3), "/prev", "prev = head")
    schema.validate("event", mut(ex.EVENTS[R], lambda e: e.update(abandoned=None)), log="ticket")
    bad("event", ev(R, abandoned={"seq": 4, "head": ex.H}), "/abandoned/seq", "above")
    bad("event", ev(R, abandoned={"seq": 9}), "/abandoned", "head")
    bad("event", ev(R, abandoned_decisions=[ex.EID, ex.EID]), "/abandoned_decisions")
    bad("event", ev(R, abandoned_decisions="x"), "/abandoned_decisions")
    bad("event", ev(R, reason=""), "/reason")
    bad("event", ev(R, abandoned_decisions=None), "", "abandoned_decisions")
    ACK = "invalid.acknowledged"
    schema.validate("event", ev(ACK, reason=None), log="ticket")
    bad("event", ev(ACK, invalid_seq=5), "/invalid_seq", "earlier")
    bad("event", ev(ACK, invalid_seq=0), "/invalid_seq")
    bad("event", ev(ACK, head="x"), "/head")
    # F1 writes the payload field as `seq`, the envelope's own name; it is `invalid_seq` here
    bad("event", ev(ACK, invalid_seq=None), "", "invalid_seq")


# ---------------------------------------------------------------- workspace log events
def test_workspace_created_genesis_links():
    W = ex.EVENTS["workspace.created"]
    bad("event", mut(W, lambda e: e["owner"].update(person="p_" + ex.PID2)), "/owner/person")
    bad("event", mut(W, lambda e: e["delegation"]["o"].update(owner_person_id=ex.PID2)), "/owner/person")
    bad("event", mut(W, lambda e: e["device_cert"]["o"].update(person_id=ex.PID2)), "/owner/person")
    bad(
        "event",
        mut(W, lambda e: e["delegation"]["o"].update(workspace_id=ex.hex32("other"))),
        "/delegation/o/workspace_id",
    )
    bad("event", mut(W, lambda e: e["delegation"]["o"].update(wsk_pub=ex.pub("other"))), "/delegation/o/wsk_pub")
    bad("event", mut(W, lambda e: e["actor"].update(device="d_" + ex.hex32("other"))), "/actor", "first device")
    bad("event", mut(W, lambda e: e["actor"].update(id="p_" + ex.PID2)), "/actor")
    bad("event", mut(W, lambda e: e["delegation"]["o"].update(kind="card")), "/delegation/o/kind")
    bad("event", mut(W, lambda e: e["delegation"]["o"].update(extra=1)), "/delegation/o", "extra")
    bad("event", mut(W, lambda e: e["delegation"].update(extra=1)), "/delegation", "extra")
    bad("event", mut(W, lambda e: e["delegation"]["o"].pop("client_hosted")), "/delegation/o", "client_hosted")
    bad("event", mut(W, lambda e: e["owner"].update(pk_pub="x")), "/owner/pk_pub")
    bad("event", mut(W, lambda e: e.update(prefix="demo")), "/prefix")
    bad("event", mut(W, lambda e: e.update(host_id="h_x")), "/host_id")
    bad("event", mut(W, lambda e: e.update(workspace_id=ex.WS[:-1])), "/workspace_id")


def test_signed_objects_and_scopes():
    M = "member.added"
    bad("event", ev(M, person="p_" + ex.PID), "/device_cert/o/person_id", "another person")
    bad("event", ev(M, role="root"), "/role")
    bad("event", ev(M, pk_pub="x"), "/pk_pub")
    bad("event", ev(M, pk_pub=ex.pub("x")[:-1] + "B"), "/pk_pub")  # trailing bits
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"].pop("sig")), "/device_cert", "sig")
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(extra=1)), "/device_cert/o", "extra")
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].pop("dk_kx_pub")), "/device_cert/o", "dk_kx_pub")
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(kind="revocation")), "/device_cert/o/kind")
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(v=1)), "/device_cert/o/v")
    bad("event", mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(suite=1)), "/device_cert/o/suite")
    bad(
        "event",
        mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(device_id=ex.DID2.upper())),
        "/device_cert/o/device_id",
    )
    bad(
        "event",
        mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(label_sealed="A")),
        "/device_cert/o/label_sealed",
    )
    bad(
        "event",
        mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(expires_ms=1)),
        "/device_cert/o/expires_ms",
        "greater",
    )
    schema.validate(
        "event",
        mut(ex.EVENTS[M], lambda e: e["device_cert"]["o"].update(expires_ms=1_760_000_000_001)),
        log="workspace",
    )
    # scopes_max is a level list that contains decide; a drop: certificate is refused
    for scopes in (
        ["look"],
        ["decide"],
        ["look", "operate"],
        ["type"],
        [],
        ["drop:" + ex.hex32("space")],
        ["look", "decide", "drop:x"],
    ):
        bad(
            "event",
            mut(ex.EVENTS["device.added"], lambda e, s=scopes: e["cert"]["o"].update(scopes_max=s)),
            "/cert/o/scopes_max",
        )
        bad(
            "event",
            mut(ex.EVENTS[M], lambda e, s=scopes: e["device_cert"]["o"].update(scopes_max=s)),
            "/device_cert/o/scopes_max",
        )
    for scopes in (["look", "decide"], ["look", "decide", "operate"], ["look", "decide", "operate", "type"]):
        schema.validate(
            "event",
            mut(ex.EVENTS["device.added"], lambda e, s=scopes: e["cert"]["o"].update(scopes_max=s)),
            log="workspace",
        )
    D = "device.added"
    bad("event", ev(D, device="d_" + ex.DID), "/cert/o/device_id", "another device")
    bad(
        "event",
        mut(ex.EVENTS[D], lambda e: e["cert"]["o"].update(person_id=ex.PID2)),
        "/cert/o/person_id",
        "same person",
    )
    # the general certificate (protocol 6.1) does allow look-only and drop: scopes, for other consumers
    v = Draft202012Validator({"$ref": schema.BASE + "common#/$defs/deviceCert"}, registry=schema._registry())
    assert v.is_valid(mut(ex.cert(ex.PID, ex.DID2), lambda c: c["o"].update(scopes_max=["drop:" + ex.hex32("s")])))
    assert v.is_valid(mut(ex.cert(ex.PID, ex.DID2), lambda c: c["o"].update(scopes_max=["look"])))
    assert not v.is_valid(mut(ex.cert(ex.PID, ex.DID2), lambda c: c["o"].update(scopes_max=["decide"])))


def test_member_device_role_events():
    bad("event", ev("member.removed", person="p_x"), "/person")
    bad("event", ev("role.changed", role="boss"), "/role")
    bad("event", ev("device.removed", device="mac"), "/device")
    schema.validate("event", ev("device.removed", reason=None), log="workspace")
    bad("event", ev("device.removed", reason=""), "/reason")
    R = "device.revoked"
    bad("event", ev(R, reason="stolen"), "/reason")
    bad("event", ev(R, reason="compromised"), "/reason", "revocation.o.reason")  # must equal the embedded one
    bad("event", ev(R, device="d_" + ex.DID), "/revocation/o/device_id", "another device")
    bad("event", ev(R, revocation=None), "", "revocation")
    bad("event", mut(ex.EVENTS[R], lambda e: e["revocation"]["o"].pop("reason")), "/revocation/o", "reason")
    bad("event", mut(ex.EVENTS[R], lambda e: e["revocation"]["o"].update(reason="stolen")), "/revocation/o/reason")
    bad("event", mut(ex.EVENTS[R], lambda e: e["revocation"]["o"].update(extra=1)), "/revocation/o", "extra")
    bad("event", mut(ex.EVENTS[R], lambda e: e["revocation"]["o"].update(kind="device_cert")), "/revocation/o/kind")
    bad("event", mut(ex.EVENTS[R], lambda e: e["revocation"].update(sig="x")), "/revocation/sig")
    for reason in ("compromised", "lost", "retired"):
        e = mut(ex.EVENTS[R], lambda e, r=reason: (e.update(reason=r), e["revocation"]["o"].update(reason=r)))
        schema.validate("event", e, log="workspace")
        schema.validate("event", _with_actor(R, "host", e), log="workspace")  # the host may append it too


def test_settings_and_grants():
    S = "settings.changed"
    bad("event", ev(S, set={}), "/set")
    bad("event", ev(S, set={"grant_hours": 0}), "/set/grant_hours")
    bad("event", ev(S, set={"grant_hours": 25}), "/set/grant_hours")
    bad("event", ev(S, set={"claim_ttl_min": 14}), "/set/claim_ttl_min")
    bad("event", ev(S, set={"claim_ttl_min": 1441}), "/set/claim_ttl_min")
    bad("event", ev(S, set={"lease_ttl_min": 4}), "/set/lease_ttl_min")
    bad("event", ev(S, set={"grant_hours": 1.0}), "/set/grant_hours", "floats")
    bad("event", ev(S, set={"theme": "dark"}), "/set", "theme")
    bad("event", ev(S, set={"repos": {"a": {"path": "x", "extra": 1}}}), "/set/repos/a")
    bad("event", ev(S, set={"repos": {"a": {}}}), "/set/repos/a")
    bad("event", ev(S, set={"repos": {"a b": None}}), "/set/repos")
    schema.validate("event", ev(S, set={"grant_hours": 1, "claim_ttl_min": 15, "lease_ttl_min": 5}), log="workspace")
    schema.validate(
        "event", ev(S, set={"grant_hours": 24, "claim_ttl_min": 1440, "lease_ttl_min": 1440}), log="workspace"
    )
    G = "grant.issued"
    bad("event", ev(G, scope="some"), "/scope")
    bad("event", ev(G, verbs="all"), "/verbs")
    bad("event", ev(G, verbs=[]), "/verbs")
    bad("event", ev(G, verbs=["Task Done"]), "/verbs/0")
    schema.validate("event", ev(G, verbs=["task.done", "ask"]), log="workspace")
    bad("event", ev(G, hours=0, expires_at="2026-10-09T09:10:00Z"), "/hours")
    bad("event", ev(G, hours=25, expires_at="2026-10-10T10:10:00Z"), "/hours")
    bad("event", ev(G, expires_at="2026-10-09T18:10:00Z"), "/expires_at", "3600")
    bad("event", ev(G, hours=8.0), "/hours", "floats")
    bad("event", ev(G, issued_at="2026-10-09T09:00:00Z", expires_at="2026-10-09T17:00:00Z"), "/issued_at", "300")
    schema.validate(
        "event", ev(G, issued_at="2026-10-09T09:05:11Z", expires_at="2026-10-09T17:05:11Z"), log="workspace"
    )
    bad("event", ev(G, issued_at="2026-10-09T09:15:12Z", expires_at="2026-10-09T17:15:12Z"), "/issued_at", "300")
    bad("event", ev(G, secret_hash="x"), "/secret_hash")
    bad("event", ev(G, secret_hash=None), "", "secret_hash")
    bad("event", ev(G, issued_at=None), "", "issued_at")
    bad("event", ev(G, grant="gr_x"), "/grant")
    bad("event", ev(G, **{"for": "p_" + ex.PID}), "", "for")  # always for the signer
    bad("event", ev("grant.revoked", grant="x"), "/grant")


def test_addon_events():
    A = "addon.granted"
    bad("event", ev(A, name="gate"), "/name")
    bad("event", ev(A, name="Estimate"), "/name")
    bad("event", ev(A, version="1.2"), "/version")
    bad("event", ev(A, version="01.2.0"), "/version")
    bad("event", ev(A, package_sha256="ab" * 32), "/package_sha256")
    bad("event", ev(A, capabilities=["root"]), "/capabilities/0")
    bad("event", ev(A, capabilities=["pty", "pty"]), "/capabilities")
    bad("event", mut(ex.EVENTS[A], lambda e: e["binds"].pop("fields")), "/binds", "fields")
    bad("event", mut(ex.EVENTS[A], lambda e: e["binds"]["fields"].update(points=[])), "/binds/fields/points")
    bad("event", mut(ex.EVENTS[A], lambda e: e["binds"]["fields"].update(points=["deploy"])), "/binds/fields/points/0")
    bad(
        "event",
        mut(ex.EVENTS[A], lambda e: e["binds"]["sections"][0].update(types=["story"])),
        "/binds/sections/0/types/0",
    )
    bad("event", mut(ex.EVENTS[A], lambda e: e["binds"]["sections"][0].pop("gate")), "/binds/sections/0", "gate")
    bad("event", mut(ex.EVENTS[A], lambda e: e["binds"].update(extra=1)), "/binds", "extra")
    schema.validate("event", mut(ex.EVENTS[A], lambda e: e["binds"].update(fields={}, sections=[])), log="workspace")
    bad("event", ev("addon.disabled", name="x" * 41), "/name")
    bad("event", ev("addon.purged", name="log"), "/name")


# ---------------------------------------------------------------- policies (5.7)
def test_policy_keys_and_independent():
    p = ex.policy()
    gate = lambda q: ev("policy.changed", gates={"plan": q})  # noqa: E731
    schema.validate("event", gate(p), log="ticket")
    for k in ("approvers", "count", "not", "applies", "independent"):
        bad("event", gate({x: v for x, v in p.items() if x != k}), "/gates/plan", k)
    bad("event", gate({**p, "independent": "yes"}), "/gates/plan/independent")
    bad("event", gate({**p, "independent": 1}), "/gates/plan/independent")
    bad("event", gate({**p, "approvers": ["viewer"]}), "/gates/plan/approvers/0")
    bad("event", gate({**p, "approvers": ["owner", "owner"]}), "/gates/plan/approvers")
    bad("event", gate({**p, "not": ["ticket_owners"]}), "/gates/plan/not/0")
    bad("event", gate({**p, "count": 1.0}), "/gates/plan/count", "floats")
    bad("event", gate({**p, "applies": []}), "/gates/plan/applies")
    bad("event", gate({**p, "applies": "none"}), "/gates/plan/applies")
    for tok in ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers"):
        schema.validate("event", gate({**p, "approvers": [tok], "not": [tok]}), log="ticket")
    for applies in ("all", "off", ["bug"], ["bug", "feature"]):
        schema.validate("event", gate({**p, "applies": applies}), log="ticket")
    schema.validate(
        "event",
        ev("policy.changed", gates={"code": ex.policy(("owner",), 2, ("assignees", "reviewers"), ["feature"], True)}),
        log="ticket",
    )


# ---------------------------------------------------------------- checkpoint, gate input, manifest, operation, cli
def test_checkpoint_invalid():
    T, W = ex.CHECKPOINT_TICKET, ex.CHECKPOINT_WORKSPACE
    bad("checkpoint", mut(T, lambda c: c["o"].pop("head")), "/o", "head")
    bad("checkpoint", mut(T, lambda c: c["o"].update(seq=0)), "/o/seq")
    bad("checkpoint", mut(T, lambda c: c.pop("sig")), "", "sig")
    bad("checkpoint", mut(T, lambda c: c.update(sig="x")), "/sig")
    bad("checkpoint", mut(T, lambda c: c.update(extra=1)), "", "extra")
    bad("checkpoint", mut(T, lambda c: c["o"].update(kind="checkpoint")), "/o/kind")
    bad("checkpoint", mut(T, lambda c: c["o"].update(v=1)), "/o/v")
    bad("checkpoint", mut(T, lambda c: c["o"].update(suite=1)), "/o/suite")
    bad("checkpoint", mut(T, lambda c: c["o"].update(extra=1)), "/o", "extra")
    bad("checkpoint", mut(T, lambda c: c["o"].update(at="2026-13-01T00:00:00Z")), "/o/at")
    bad("checkpoint", mut(T, lambda c: c["o"].update(at="2026-02-30T00:00:00Z")), "/o/at", "calendar")
    bad("checkpoint", mut(T, lambda c: c["o"].update(workspace_id="x")), "/o/workspace_id")
    bad("checkpoint", mut(T, lambda c: c["o"].update(**{"genesis": ex.H})), "/o", "genesis")
    bad("checkpoint", mut(W, lambda c: c["o"].update(n=0)), "/o/n")
    bad("checkpoint", mut(W, lambda c: c["o"].pop("genesis")), "/o", "genesis")
    bad("checkpoint", mut(W, lambda c: c["o"].pop("workspace_log")), "/o", "workspace_log")
    bad("checkpoint", mut(W, lambda c: c["o"]["workspace_log"].update(seq=0)), "/o/workspace_log/seq")
    bad("checkpoint", mut(W, lambda c: c["o"]["tickets"].update({"x": {"seq": 1, "head": ex.H}})), "/o/tickets")
    bad("checkpoint", mut(W, lambda c: c["o"]["tickets"][ex.U1].pop("head")), "/o/tickets/" + ex.U1, "head")
    bad("checkpoint", mut(W, lambda c: c["o"].update(heads={})), "/o", "heads")
    bad("checkpoint", {"v": 2, "uid": ex.U1, "seq": 9, "head": ex.H, "host_sig": ex.SIG}, "", "o")  # the 8 Oct shape
    bad("checkpoint", {"o": {"kind": "other"}, "sig": ex.SIG}, "/o/kind")


def test_gate_input():
    G = ex.GATE_INPUT
    assert len(schema.load("gate-input")["required"]) == 15
    for k in schema.load("gate-input")["required"]:
        bad("gate-input", mut(G, lambda o, k=k: o.pop(k)), "", k)
    bad("gate-input", mut(G, lambda o: o.update(extra=1)), "", "extra")
    bad("gate-input", mut(G, lambda o: o["artifacts"]["a.png"].update(sha256=ex.H)), "/artifacts/a.png", "sha256")
    bad("gate-input", mut(G, lambda o: o["artifacts"]["a.png"].pop("task")), "/artifacts/a.png", "task")
    bad("gate-input", mut(G, lambda o: o["artifacts"]["a.png"].update(digest="x")), "/artifacts/a.png/digest")
    bad("gate-input", mut(G, lambda o: o["receipts"]["T2"].pop("commit")), "/receipts/T2", "commit")
    bad("gate-input", mut(G, lambda o: o["receipts"]["T2"].update(event="x")), "/receipts/T2/event")
    bad("gate-input", mut(G, lambda o: o.update(hash_v=2)), "/hash_v")
    bad("gate-input", mut(G, lambda o: o.update(schema="orch.ticket/1")), "/schema")
    bad("gate-input", mut(G, lambda o: o["fields"].pop("addons")), "/fields", "addons")
    # per-gate emptiness
    bad("gate-input", mut(G, lambda o: o["fields"].update(links=None)), "/fields/links")
    bad("gate-input", mut(G, lambda o: o["sections"].update(plan=ex.H)), "/sections/plan", "not a section of verify")
    bad(
        "gate-input",
        mut(G, lambda o: o["tasks"].append({"id": "T1", "text": "x", "verify": None, "proves": []})),
        "/tasks",
    )
    bad(
        "gate-input",
        mut(G, lambda o: o["prior"].update(verify={"gen": 0, "approvals": []})),
        "/prior/verify",
        "earlier",
    )
    bad(
        "gate-input",
        mut(G, lambda o: o["prior"]["plan"].update(approvals=[ex.EID, "01J9ZPABCDEFGHJKMNPQRSTVWA"])),
        "/prior/plan/approvals",
        "sorted",
    )
    bad("gate-input", mut(G, lambda o: o["source_sha"].append({**o["source_sha"][0]})), "/source_sha", "sorted")
    req = mut(
        G,
        lambda o: (
            o.update(
                gate="requirements",
                receipts={},
                source_sha=[],
                prior={},
                sections={"summary": ex.H, "requirements": ex.H},
                artifacts={},
            )
            or o["fields"].update(links=None)
        ),
    )
    schema.validate("gate-input", req)
    bad(
        "gate-input",
        mut(req, lambda o: o["prior"].update(requirements={"gen": 0, "approvals": []})),
        "/prior/requirements",
        "earlier",
    )
    bad("gate-input", mut(req, lambda o: o.update(receipts=G["receipts"])), "/receipts")
    bad("gate-input", mut(req, lambda o: o.update(source_sha=ex.SOURCE)), "/source_sha")
    bad("gate-input", mut(req, lambda o: o["sections"].update(verification=ex.H)), "/sections/verification")
    plan = mut(
        req,
        lambda o: o.update(
            gate="plan",
            sections={"plan": ex.H, "decisions": ex.H},
            tasks=[{"id": "T1", "text": "a", "verify": None, "proves": []}],
            prior={"requirements": {"gen": 1, "approvals": [ex.EID]}},
        ),
    )
    schema.validate("gate-input", plan)
    code = mut(G, lambda o: o.update(gate="code", sections={}, artifacts={}, receipts={}))
    schema.validate("gate-input", code)
    bad(
        "gate-input",
        mut(code, lambda o: o["sections"].update(verification=ex.H)),
        "/sections/verification",
        "no sections",
    )
    bad("gate-input", mut(code, lambda o: o.update(artifacts=G["artifacts"])), "/artifacts")


def test_manifest_invalid():
    M = ex.MANIFEST
    bad("addon-manifest", mut(M, lambda m: m.pop("name")), "", "name")
    for k in ("schema", "version", "title", "entry"):
        bad("addon-manifest", mut(M, lambda m, k=k: m.pop(k)), "", k)
    schema.validate("addon-manifest", {k: M[k] for k in ("schema", "name", "version", "title", "entry")})
    bad("addon-manifest", mut(M, lambda m: m.update(hooks=[])), "", "hooks")
    bad("addon-manifest", mut(M, lambda m: m.update(version="1.2")), "/version")
    bad("addon-manifest", mut(M, lambda m: m.update(name="Estimate")), "/name")
    bad("addon-manifest", mut(M, lambda m: m.update(name="x" * 41)), "/name")
    bad("addon-manifest", mut(M, lambda m: m.update(title="")), "/title")
    bad("addon-manifest", mut(M, lambda m: m.update(title="x" * 41)), "/title")
    for ch in "()·:":
        bad("addon-manifest", mut(M, lambda m, ch=ch: m.update(title="a" + ch + "b")), "/title")
    bad("addon-manifest", mut(M, lambda m: m.update(title="a​b")), "/title", "invisible")
    bad("addon-manifest", mut(M, lambda m: m.update(title="a﻿b")), "/title", "invisible")
    schema.validate("addon-manifest", mut(M, lambda m: m.update(title="É" * 40)))
    bad("addon-manifest", mut(M, lambda m: m["entry"].update(cmd=[])), "/entry/cmd")
    bad("addon-manifest", mut(M, lambda m: m.update(entry={"cmd": "python"})), "/entry/cmd")
    bad("addon-manifest", mut(M, lambda m: m["fields"]["points"].update(set_by=["root"])), "/fields/points")
    bad("addon-manifest", mut(M, lambda m: m["fields"]["points"].pop("set_by")), "/fields/points")
    bad("addon-manifest", mut(M, lambda m: m["fields"]["points"].update(type="float")), "/fields/points")
    bad("addon-manifest", mut(M, lambda m: m["fields"]["points"].update(values=["a"])), "/fields/points")  # enum only
    bad(
        "addon-manifest",
        mut(M, lambda m: m["fields"]["points"].update(min=50, max=10)),
        "/fields/points/min",
        "greater",
    )
    bad("addon-manifest", mut(M, lambda m: m["fields"]["points"].update(gate=["deploy"])), "/fields/points")
    bad(
        "addon-manifest",
        mut(M, lambda m: m["fields"].update({"Bad": {"type": "boolean", "set_by": ["agent"]}})),
        "/fields",
    )
    bad("addon-manifest", mut(M, lambda m: m["fields"]["mood"].pop("values")), "/fields/mood")
    bad("addon-manifest", mut(M, lambda m: m["fields"]["mood"].update(values=[])), "/fields/mood")
    bad(
        "addon-manifest",
        mut(M, lambda m: m["fields"].update(name={"type": "string", "set_by": ["agent"], "max_len": 201})),
        "/fields/name/max_len",
    )
    bad(
        "addon-manifest",
        mut(M, lambda m: m["fields"].update(name={"type": "text", "set_by": ["agent"], "max_len": 4097})),
        "/fields/name",
    )
    for ftype in ("string", "text", "boolean", "person"):
        schema.validate(
            "addon-manifest",
            mut(M, lambda m, t=ftype: m["fields"].update(f={"type": t, "set_by": ["agent", "addon", "ticket_owner"]})),
        )
    schema.validate(
        "addon-manifest",
        mut(M, lambda m: m["fields"].update(f={"type": "string_list", "set_by": ["agent"], "max_items": 5})),
    )
    bad("addon-manifest", mut(M, lambda m: m["sections"][0].update(types=["story"])), "/sections/0/types/0")
    bad("addon-manifest", mut(M, lambda m: m["sections"][0].update(after="nope")), "/sections/0/after")
    bad("addon-manifest", mut(M, lambda m: m["sections"][0].pop("heading")), "/sections/0", "heading")
    bad("addon-manifest", mut(M, lambda m: m["sections"][0].update(heading="x" * 41)), "/sections/0/heading")
    bad("addon-manifest", mut(M, lambda m: m["sections"][0].update(title="x")), "/sections/0", "title")
    bad(
        "addon-manifest",
        mut(M, lambda m: m["sections"].append(ex.fresh(m["sections"][0]))),
        "/sections/1/id",
        "duplicate",
    )
    bad(
        "addon-manifest",
        mut(M, lambda m: m["artifact_kinds"].append({"kind": "chart", "label": "x"})),
        "/artifact_kinds/1/kind",
        "duplicate",
    )
    bad(
        "addon-manifest",
        mut(M, lambda m: m["artifact_kinds"].append({"kind": "screenshot", "label": "x"})),
        "/artifact_kinds/1/kind",
    )
    bad("addon-manifest", mut(M, lambda m: m["artifact_kinds"].append("chart")), "/artifact_kinds/1")
    bad("addon-manifest", mut(M, lambda m: m.update(capabilities="pty")), "/capabilities")
    bad("addon-manifest", mut(M, lambda m: m.update(capabilities=["root"])), "/capabilities/0")
    bad("addon-manifest", mut(M, lambda m: m.update(capabilities=["pty", "pty"])), "/capabilities")
    for c in ("serve_http", "spawn_agent", "pty", "network", "git_push"):
        schema.validate("addon-manifest", mut(M, lambda m, c=c: m.update(capabilities=[c])))
    # deferred to C9: accepted only empty
    for k, empty, full in (
        ("needs", [], [{"id": "x"}]),
        ("cli", {}, {"group": "e", "ops": []}),
        ("skills", [], ["a.md"]),
        ("agents_md", "", "x"),
    ):
        schema.validate("addon-manifest", mut(M, lambda m, k=k, e=empty: m.update({k: e})))
        bad("addon-manifest", mut(M, lambda m, k=k, f=full: m.update({k: f})), "/" + k)


def test_operation_invalid():
    for k in ("name", "input", "who", "pre", "emits", "output", "errors"):
        bad("operation", mut(ex.OPERATION, lambda o, k=k: o.pop(k)), "", k)
    bad("operation", mut(ex.OPERATION, lambda o: o.update(who="root")), "/who")
    bad("operation", mut(ex.OPERATION, lambda o: o.update(extra=1)), "", "extra")
    bad("operation", mut(ex.OPERATION, lambda o: o.update(emits="task.done")), "/emits")
    bad("operation", mut(ex.OPERATION, lambda o: o["input"].update(type="array")), "/input/type")
    bad("operation", mut(ex.OPERATION, lambda o: o["input"].update(properties=5)), "/input/properties")
    bad("operation", mut(ex.OPERATION, lambda o: o["output"].update(text="done")), "/output/text")
    bad("operation", mut(ex.OPERATION, lambda o: o["output"].update(text="ok a\nnext: b\nnext: c")), "/output/text")
    bad("operation", mut(ex.OPERATION, lambda o: o["output"].update(text="ok a\n")), "/output/text")
    bad("operation", mut(ex.OPERATION, lambda o: o["errors"][0].pop("fix")), "/errors/0", "fix")
    bad("operation", mut(ex.OPERATION, lambda o: o["errors"][0]["fix"].update(argv=[])), "/errors/0/fix/argv")


def test_cli_envelopes_invalid():
    bad("cli-result", mut(ex.CLI_RESULT, lambda r: r.update(ok=False)), "/ok")
    bad("cli-result", mut(ex.CLI_RESULT, lambda r: r.pop("hints")), "", "hints")
    bad("cli-result", mut(ex.CLI_RESULT, lambda r: r.update(key="demo-1")), "/key")
    schema.validate("cli-result", mut(ex.CLI_RESULT, lambda r: r.update(key=None, duplicate=True)))
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r["error"].pop("code")), "/error", "code")
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r["error"].update(retryable="no")), "/error/retryable")
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r["error"].update(code="Human Only")), "/error/code")
    bad("cli-error", mut(ex.CLI_ERROR, lambda r: r.update(ok=True)), "/ok")


@pytest.mark.parametrize("kind", list(ex.WAIT))
def test_wait_result_valid(kind):
    schema.validate("wait-result", ex.WAIT[kind])


def test_wait_result_invalid():
    W = ex.WAIT
    bad("wait-result", {**W["timeout"], "kind": "done"}, "/kind")
    bad("wait-result", {"kind": "timeout"}, "", "required")
    bad("wait-result", mut(W["timeout"], lambda r: r.pop("key")), "", "key")
    bad("wait-result", mut(W["timeout"], lambda r: r.update(seq=3)), "", "seq")  # absent on timeout
    bad("wait-result", mut(W["timeout"], lambda r: r.update(by="p_" + ex.PID)), "", "by")
    bad("wait-result", mut(W["approved"], lambda r: r.pop("seq")), "", "seq")
    bad("wait-result", mut(W["approved"], lambda r: r.pop("by")), "", "by")
    bad("wait-result", mut(W["approved"], lambda r: r.pop("gate")), "", "gate")
    bad("wait-result", mut(W["approved"], lambda r: r.update(gate="verify")), "/gate")
    bad("wait-result", mut(W["approved"], lambda r: r.update(text="x")), "", "text")  # nothing else
    bad("wait-result", mut(W["approved"], lambda r: r.update(question="Q1")), "", "question")
    bad("wait-result", mut(W["answered"], lambda r: r.pop("question")), "", "question")
    bad("wait-result", mut(W["answered"], lambda r: r.pop("option")), "")  # option or text
    schema.validate("wait-result", mut(W["answered"], lambda r: [r.pop("option"), r.update(text="csv")]))
    bad("wait-result", mut(W["answered"], lambda r: r.update(gate="plan")), "", "gate")
    bad("wait-result", mut(W["changes_requested"], lambda r: r.pop("text")), "", "text")
    bad("wait-result", mut(W["verdict"], lambda r: r.pop("text")), "", "text")  # required on fail
    bad("wait-result", mut(W["verdict"], lambda r: r.update(outcome="pass")), "", "text")  # only with fail
    schema.validate("wait-result", mut(W["verdict"], lambda r: [r.update(outcome="pass"), r.pop("text")]))
    bad("wait-result", mut(W["verdict"], lambda r: r.update(outcome="maybe")), "/outcome")
    bad("wait-result", mut(W["verdict"], lambda r: r.update(gate="verify")), "", "gate")
    bad("wait-result", mut(W["invalidated"], lambda r: r.update(by="p_" + ex.PID)), "", "by")  # no human decision
    bad("wait-result", mut(W["invalidated"], lambda r: r.pop("gate")), "", "gate")
    schema.validate("wait-result", mut(W["invalidated"], lambda r: r.update(gate="code")))
    bad("wait-result", mut(W["invalidated"], lambda r: r.update(gate="deploy")), "/gate")
    bad("wait-result", mut(W["approved"], lambda r: r.update(key="demo")), "/key")
    bad("wait-result", mut(W["approved"], lambda r: r.update(seq=0)), "/seq")


# ---------------------------------------------------------------- ids and encodings (11.1)
def _def_valid(name, value):
    v = Draft202012Validator({"$ref": schema.BASE + "common#/$defs/" + name}, registry=schema._registry())
    return v.is_valid(value)


@pytest.mark.parametrize(
    "name,good,not_good",
    [
        (
            "ulid",
            [ex.U1],
            [
                "01J9ZK4Q7M3R8T2V6X0B5N1C9",
                ex.U1.lower(),
                "81J9ZK4Q7M3R8T2V6X0B5N1C9D",
                "01J9ZK4Q7M3R8T2V6X0B5N1C9U",
                ex.U1 + "\n",
            ],
        ),
        ("hex32", [ex.WS], [ex.WS.upper(), ex.WS[:-1], ex.WS + "0", "g" * 32]),
        ("personId", ["p_" + ex.PID], ["p_sev", "p_" + ex.PID.upper(), ex.PID, "p_" + ex.PID[:-1]]),
        ("deviceId", ["d_" + ex.DID], ["d_mac", "d_" + ex.DID + "0"]),
        ("hostId", ["h_" + ex.U1], ["h_01J9Z7", "h_" + ex.U1.lower()]),
        ("grantId", ["gr_" + ex.U1], ["gr_x", "g_" + ex.U1]),
        (
            "sessionId",
            ["s_" + ex.U1, "s_" + ex.U1 + ".2", "s_" + ex.U1 + ".1.2.3"],
            ["s_x", "s_" + ex.U1 + ".0", "s_" + ex.U1 + ".01", "s_" + ex.U1 + ".1.2.3.4", "s_" + ex.U1 + ".10000"],
        ),
        ("agentId", ["claude-code", "codex", "ci"], ["Claude", "1ci", "a" * 33, "claude_code"]),
        ("acId", ["AC1", "AC12"], ["AC0", "AC01", "ac1", "AC"]),
        ("taskId", ["T1", "T20"], ["T0", "T01", "t1"]),
        ("questionId", ["Q1", "Q9"], ["Q0", "Q"]),
        ("token", ["a", "a_b1"], ["A", "1a", "a-b", "a b"]),
        ("hash", [ex.H], ["sha256:" + "a" * 63, "sha256:" + "A" * 64, "a" * 64, "sha1:" + "a" * 64]),
        ("gitCommit", ["a" * 40, "a" * 64], ["a" * 7, "a" * 41, "A" * 40, "a" * 39]),
        (
            "timestamp",
            ["2026-10-09T09:10:11Z", "2026-10-09T09:10:59Z"],
            [
                "2026-10-09T09:10:60Z",
                "2026-10-09T24:00:00Z",
                "2026-10-09T09:10:11+00:00",
                "2026-10-09T09:10:11.0Z",
                "2026-10-09 09:10:11Z",
            ],
        ),
        ("date", ["2026-10-09"], ["2026-10-9", "2026-13-09", "20261009"]),
        ("prefix", ["DEMO", "A", "A1B2"], ["demo", "1A", "A" * 17, "DE-MO"]),
        ("b64u", ["", "AA", "AAA", "AAAA", "AQ", "-_-_"], ["A", "AAAAA", "AB", "AAB", "AA=", "a+b/", "AA==", "AAF"]),
        (
            "repoIdentity",
            ["https://github.com/acme/x", "https://h:8443/a/b", "local:acme-x"],
            ["http://x/y", "file:///x", "git@github.com:a/b", "local:", "https://u:t@h/x y"],
        ),
        ("artifactName", ["a.png", "A-b_c.1"], ["-a", ".a", "a/b", "a b", "a" * 129]),
    ],
)
def test_id_forms(name, good, not_good):
    for g in good:
        assert _def_valid(name, g), (name, g)
    for b in not_good:
        assert not _def_valid(name, b), (name, b)


def test_signature_and_key_encodings():
    assert _def_valid("sig", ex.SIG) and _def_valid("pubkey", ex.pub())
    assert not _def_valid("sig", ex.pub())  # 65 bytes is not a signature
    assert not _def_valid("pubkey", ex.SIG)
    assert not _def_valid("pubkey", "A" + ex.pub()[1:])  # not an uncompressed point (first byte 0x04)
    assert not _def_valid("sig", "p256:" + ex.SIG)
    assert not _def_valid("sig", ex.SIG + "\n")
    assert not _def_valid("sig", ex.SIG[:-1] + "B")  # non-zero trailing bits
    assert not _def_valid("pubkey", ex.pub()[:-1] + "B")


# ---------------------------------------------------------------- doc coverage (strict, both directions)
def test_every_schema_is_mapped_to_the_doc():
    non_event = {n for n in schema.names() if not n.startswith("event.")}
    assert non_event == set(cov.SCHEMA_TO_DOC)


def test_doc_headings_exist():
    text = DOC.read_text()
    for h in cov.DOC_HEADINGS:
        assert re.search(rf"^{re.escape(h)}\s*$", text, re.M), h


def test_event_types_match_doc_tables():
    """Every event type named in the F1 tables has a schema, and every event schema is in a table."""
    ticket, ws = set(TICKET_ROWS), set(WS_ROWS)
    assert len(ticket) >= 25 and len(ws) >= 14  # the table parser found the rows
    have = {n[len("event.") :] for n in schema.names() if n.startswith("event.")}
    assert have == ticket | ws
    shared = ticket & ws | {t for t, r in {**TICKET_ROWS, **WS_ROWS}.items() if "Both logs" in r[2]}
    assert shared == set(schema._BOTH_LOGS)
    assert set(schema.event_types("ticket")) == ticket
    both = {t for t, r in TICKET_ROWS.items() if "Both logs" in r[2]}
    assert set(schema.event_types("workspace")) == ws | both
    assert not (ticket & ws) - set(schema._BOTH_LOGS)


# Names in the doc that look like event types but are not: error codes, ticket.json paths, config paths.
DOC_NON_EVENTS = {
    "artifact.mismatch", "claim.not_live", "gate.incomplete", "gate.no_eligible", "gate.stale", "gate.suspicious_text",
    "settings.repos", "ticket.acceptance", "ticket.addons", "ticket.json", "ticket.key", "ticket.links",
    "ticket.questions", "ticket.schema", "ticket.size", "ticket.tasks", "ticket.title", "ticket.type", "ticket.uid",
    "ticket.visibility",
}  # fmt: skip


def test_doc_event_names_in_text_have_schemas():
    """Every `x.y` with a reserved event prefix in the doc (outside the decisions log) is a schema or listed above."""
    text = DOC.read_text()
    text = text[: text.index("## 13. Decisions log")]
    prefixes = "|".join(schema.load("common")["$defs"]["reservedPrefix"]["enum"])
    found = set(re.findall(r'"type":"([a-z.]+)"', text)) | set(re.findall(rf"`((?:{prefixes})\.[a-z_]+)`", text))
    known = {n[len("event.") :] for n in schema.names() if n.startswith("event.")}
    assert {k for k in known if "." in k} <= found, known - found  # and every schema is named in the doc
    assert not (found - known - DOC_NON_EVENTS), found - known - DOC_NON_EVENTS
    assert not DOC_NON_EVENTS & known


def _row_fields(cell):
    """Top-level payload field names of a Payload cell, with whether each is optional."""
    if cell.strip() == "(none)":
        return {}
    chunks, depth, cur = [], 0, ""
    for ch in cell:
        depth += ch in "{[("
        depth -= ch in "}])"
        if ch == ";" and depth == 0:
            chunks.append(cur)
            cur = ""
        else:
            cur += ch
    chunks.append(cur)
    out = {}
    for c in chunks:
        m = re.search(r"`([a-z0-9_]+)(\?)?`", c)
        assert m, (cell, c)
        out[m.group(1)] = bool(m.group(2))
    return out


@pytest.mark.parametrize("t", sorted(set(TICKET_ROWS) | set(WS_ROWS)))
def test_payload_fields_match_the_doc(t):
    row = TICKET_ROWS.get(t) or WS_ROWS[t]
    s = schema.load("event." + t)
    env = set(cov.ENVELOPE_FIELDS)
    props = {k for k in s["properties"] if k not in env}
    required = set(s["required"]) - env
    if t in cov.IRREGULAR_PAYLOAD_CELLS:
        doc_required, doc_optional = cov.ARTIFACT_FIELDS[t]
        assert props == doc_required | doc_optional
        assert required == doc_required
        return
    doc = _row_fields(row[1])
    renamed = cov.RENAMED_PAYLOAD_FIELDS.get(t, {})
    doc = {renamed.get(k, k): v for k, v in doc.items()}
    assert set(doc) == props, (t, set(doc) ^ props)
    # fields marked "?" are optional; the others are required, except where the doc makes them conditional
    conditional = {"gate.approved": {"source_sha"}, "verdict.given": set()}.get(t, set())
    for f, optional in doc.items():
        if f in conditional:
            continue
        assert (f in required) == (not optional), (t, f)
    assert not env & set(doc), (t, "payload reuses an envelope name")


def test_envelope_fields_match_the_doc_table():
    text = DOC.read_text()
    block = text[text.index("### 5.1 The envelope") : text.index("Then the payload fields of the type")]
    doc = set(re.findall(r"^\| `([a-z_]+)` \|", block, re.M))
    assert doc == set(cov.ENVELOPE_FIELDS)
    assert set(schema.load("event")["properties"]) == set(cov.ENVELOPE_FIELDS)


def test_ticket_keys_match_the_doc_table():
    text = DOC.read_text()
    block = text[text.index("**Every key is always present.**") : text.index("The `spike` type covers")]
    doc = set(re.findall(r"^\| `([a-z_]+)` \|", block, re.M)) | {"schema", "uid", "key"}
    assert len(doc) == 17 and doc == set(schema.load("ticket")["properties"])
    defaults = {
        "priority": "medium", "size": None, "labels": [], "parent": None, "blocked_by": [], "due": None,
        "visibility": "workspace", "links": {"repos": [], "branches": {}, "prs": [], "external": []},
        "acceptance": [], "tasks": [], "questions": [], "addons": {},
    }  # fmt: skip
    for k, v in defaults.items():
        assert ex.TICKET_NEW[k] == v


def test_value_lists_match_the_doc():
    text = DOC.read_text()
    block = text[text.index("### 11.4 Value lists") : text.index("### 11.5")]
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) == 2 and cells[0] not in ("List", "---"):
            rows[cells[0]] = cells[1]
    for row, (sname, dname) in cov.VALUE_LISTS.items():
        values = [v for v in re.findall(r"`([a-z_-]+)`", rows[row]) if v != "null"]
        assert set(values) == set(schema.load(sname)["$defs"][dname]["enum"]), row
    s = schema.load("ticket")["properties"]
    assert set(re.findall(r"`([a-z]+)`", rows["priority"])) - {"medium"} | {"medium"} == set(s["priority"]["enum"])
    assert {v for v in re.findall(r"`([a-z]+)`", rows["size"]) if v != "null"} == set(s["size"]["oneOf"][0]["enum"])
    kinds = {
        v["properties"]["type"]["const"]
        for v in schema.load("addon-manifest")["properties"]["fields"]["additionalProperties"]["oneOf"]
    }
    assert kinds == set(re.findall(r"`([a-z_]+)`", rows["manifest field type"]))
    assert set(re.findall(r"`([a-z-]+)`", rows["`auth`"])) == set(schema.load("common")["$defs"]["auth"]["enum"])


def test_reserved_prefixes_match_the_doc():
    text = DOC.read_text()
    block = text[text.index("**Unknown types.**") : text.index("**Not events.**")]
    listed = set(re.findall(r"`([a-z]+)`", block.split("are reserved")[0]))
    assert listed == set(schema.load("common")["$defs"]["reservedPrefix"]["enum"])
    prefixes = {t.split(".")[0] for t in schema.event_types()}
    assert prefixes <= listed


def test_policy_keys_match_the_doc():
    text = DOC.read_text()
    block = text[
        text.index("**Policy** (workspace default and ticket override)") : text.index("- **Approver tokens:**")
    ]
    doc = set(re.findall(r"^\| `([a-z_]+)` \|", block, re.M))
    assert (
        doc
        == set(schema.load("common")["$defs"]["gatePolicy"]["properties"])
        == {"approvers", "count", "not", "applies", "independent"}
    )


def test_gate_input_keys_match_the_doc():
    text = DOC.read_text()
    block = text[text.index("**Gate hash input `G`.**") : text.index("| Gate | Sections |")]
    doc = set(re.findall(r"^\| `([a-z_]+)` \|", block, re.M))
    assert doc == set(schema.load("gate-input")["properties"]) and len(doc) == 15


def test_body_table_matches_the_doc():
    text = DOC.read_text()
    block = text[text.index("| Section (heading) | id |") : text.index("Limits: 65 536 bytes")]
    cols = ["feature", "bug", "chore", "spike", "epic"]
    doc = {t: set() for t in cols}
    for line in [x for x in block.splitlines() if x.startswith("|")][2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        sid = cells[1].strip("`")
        for t, cell in zip(cols, cells[2:], strict=True):
            if cell != "—":
                doc[t].add(sid)
    assert {t: set(v) for t, v in schema.SECTIONS_BY_TYPE.items()} == doc
    assert set(schema.load("common")["$defs"]["sectionId"]["enum"]) == set().union(*doc.values())
