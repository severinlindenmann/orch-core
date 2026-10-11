"""section set, set and apply: ``base_rev`` tracked per session (F1 5.8, 10.4 item 8), text sources, atomic batches."""

from __future__ import annotations

import json

from tests.ops.helpers import OTHER, SESSION, Cli


def claimed(cli, title="A ticket", kind="feature"):
    cli("new", title, "--type", kind)
    cli("claim", "1")


def batch(*ops, ref=None):
    return json.dumps({**({"ref": ref} if ref else {}), "ops": list(ops)})


# ---------------------------------------------------------------------------------------------------- section set


def test_section_set_replaces_one_section(ws, cli):
    claimed(cli)
    r = cli("section", "set", "plan", "-m", "step one\r\nstep two")
    assert r.first == "ok DEMO-0001 ticket.updated plan seq=3" and r.out.splitlines()[-1].startswith("next:")
    body = (ws.root / "tickets" / ws.uid("1") / "body.md").read_text()
    assert "## Plan\n\nstep one\nstep two\n" in body
    assert cli.j("section", "set", "plan", "--file", "-", stdin="from stdin\n\n").code == 0
    assert "from stdin" in cli("show", "1", "--section", "plan").out
    assert cli.j("section", "set", "plan", "-m", "").code == 0  # empty: the section is cleared, not removed


def test_section_set_refusals(ws, cli):
    claimed(cli)
    r = cli.j("section", "set", "findings", "-m", "x")  # a feature has no findings
    assert (r.code, r.err_code) == (5, "invalid.input")
    r = cli.j("section", "set", "plan", "-m", "see (artifact:missing.png)")
    assert (r.code, r.err_code) == (5, "invalid.input") and "unknown" in r.doc["error"]["message"]
    r = cli.j("section", "set", "plan", "-m", "bad \x07 text")
    assert (r.code, r.err_code) == (6, "parse.text")
    r = cli.j("section", "set", "plan", "-m", "x" * 70000)
    assert (r.code, r.err_code) == (5, "invalid.input")
    assert cli.j("section", "set", "plan").err_code == "invalid.input"  # exactly one of -m / --file


def test_text_from_a_file_with_a_grant_secret_is_refused_before_anything_is_written(ws, cli, tmp_path):
    claimed(cli)
    n = len(ws.events("1"))
    f = tmp_path / "notes.md"
    f.write_text("my key is gr_01J9ZK4Q7M3R8T2V6X0B5N1C9D." + "A" * 43)
    for argv in (
        ["section", "set", "plan", "--file", str(f)],
        ["log", "--file", str(f)],
        ["handoff", "--file", str(f)],
    ):
        r = cli.j(*argv)
        assert (r.code, r.err_code) == (2, "grant.secret_in_args"), argv
    r = cli.j("log", "--file", "-", stdin=f"the real one: {ws.grant}")
    assert (r.code, r.err_code) == (2, "grant.secret_in_args") and ws.grant.partition(".")[2] not in r.out
    assert len(ws.events("1")) == n


def test_files_must_be_regular_utf8_and_small(ws, cli, tmp_path):
    claimed(cli)
    assert cli.j("log", "--file", str(tmp_path / "missing")).err_code == "invalid.input"
    assert cli.j("log", "--file", str(tmp_path)).err_code == "invalid.input"  # a directory
    (tmp_path / "bin").write_bytes(b"\xff\xfe")
    assert cli.j("log", "--file", str(tmp_path / "bin")).err_code in ("parse.text", "invalid.input")
    (tmp_path / "big").write_bytes(b"a" * (2 << 20))
    assert cli.j("log", "--file", str(tmp_path / "big")).err_code == "invalid.input"


# ---------------------------------------------------------------------------------------------------- base_rev


def test_a_section_that_changed_since_the_session_read_it_is_a_conflict(ws, cli):
    claimed(cli)
    cli("section", "set", "plan", "-m", "v1")
    other = Cli(ws, session=OTHER)
    other("show", "1", "--section", "plan")
    assert other("section", "set", "plan", "-m", "v2 by other", "--ref", "1").code == 0
    r = cli.j("section", "set", "plan", "-m", "v2 by me")
    assert (r.code, r.err_code) == (8, "conflict.section") and r.doc["error"]["retryable"] is True
    assert "section set" not in r.doc["error"]["message"] and "changed" in r.doc["error"]["message"]
    assert "v2 by other" in cli("show", "1", "--section", "plan").out  # re-reading ...
    assert cli("section", "set", "plan", "-m", "v3 by me").code == 0  # ... fixes it
    assert cli("section", "set", "plan", "-m", "v4 by me").code == 0  # a session knows what it just wrote


def test_a_section_the_session_never_read_is_refused_until_it_reads_it(ws, cli):
    claimed(cli)
    other = Cli(ws, session=OTHER)
    r = other.j("section", "set", "plan", "-m", "blind", "--ref", "1")
    assert (r.code, r.err_code) == (8, "conflict.section") and "not read" in r.doc["error"]["message"]
    other("show", "1", "--section", "plan")
    assert other("section", "set", "plan", "-m", "informed", "--ref", "1").code == 0


def test_a_field_that_changed_is_a_field_conflict(ws, cli):
    claimed(cli)
    other = Cli(ws, session=OTHER)
    other("show", "1")
    assert cli("set", "1", "title=Renamed").code == 0
    r = other.j("set", "1", "title=Mine")
    assert (r.code, r.err_code) == (8, "conflict.field")
    other("show", "1")
    assert other("set", "1", "title=Mine").code == 0
    r = other.j("ac", "add", "x", "--ref", "1")  # acceptance was shown by the default view too
    assert r.code == 0


def test_base_revs_are_per_session(ws, cli):
    claimed(cli)
    cli("section", "set", "plan", "-m", "mine")
    sub = Cli(ws, session=SESSION + ".1")
    r = sub.j("section", "set", "plan", "-m", "sub")
    assert (r.code, r.err_code) == (8, "conflict.section")  # the subagent has read nothing


# ---------------------------------------------------------------------------------------------------- set


def test_set_changes_fields_and_none_clears(ws, cli):
    claimed(cli)
    cli("new", "Parent-to-be")
    r = cli.j("set", "1", "title=Fix a, b", "priority=high", "size=m", "labels=x,y.z", "due=2026-12-31", "parent=2")
    assert r.code == 0 and r.data == {"fields": ["title", "priority", "size", "labels", "due", "parent"]}
    t = ws.ticket_json("1")
    assert (t["title"], t["priority"], t["size"], t["labels"], t["due"], t["parent"]) == (
        "Fix a, b",
        "high",
        "m",
        ["x", "y.z"],
        "2026-12-31",
        "DEMO-0002",
    )
    assert cli("set", "1", "size=none", "due=none", "labels=none", "parent=none").code == 0
    t = ws.ticket_json("1")
    assert (t["size"], t["due"], t["labels"], t["parent"]) == (None, None, [], None)
    assert cli("set", "1", "blocked_by=2").code == 0 and ws.ticket_json("1")["blocked_by"] == ["DEMO-0002"]
    r = cli("set", "1", 'links={"repos":[]}')
    assert r.code == 0


def test_set_refusals(ws, cli):
    claimed(cli)
    for pair in (
        "priority=urgent!",
        "size=xxl",
        "due=2026-13-01",
        "due=31.12.2026",
        "labels=Upper",
        "parent=999",
        "blocked_by=1,1",
        "title=" + "x" * 201,
        "links=[]",
        'links={"nope":1}',
    ):
        r = cli.j("set", "1", pair)
        assert r.code in (2, 5, 6), pair  # not_found for a missing ticket, invalid.input, parse.json
        assert r.err_code in ("invalid.input", "not_found", "parse.json"), pair
    assert cli.j("set", "1", "title=a", "title=b").err_code == "invalid.input"
    assert cli.j("set", "1", "visibility=x").code == 5  # person-only fields have their own operations
    assert cli.j("set", "1", "parent=1").err_code in ("invalid.input", "not_found")  # a ticket is not its own parent?


# ---------------------------------------------------------------------------------------------------- apply


def test_apply_runs_a_batch_in_order_on_the_state_the_earlier_items_made(ws, cli):
    claimed(cli)
    doc = batch(
        {"op": "ac.add", "text": "it works"},
        {"op": "task.add", "text": "build it", "proves": ["AC1"], "verify": "true"},
        {"op": "task.start", "task": "T1"},
        {"op": "section.set", "section": "plan", "message": "build, then test"},
        {"op": "log", "text": "batch applied"},
        {"op": "ask", "text": "ok?", "options": ["yes", "no"]},
    )
    r = cli("apply", "--file", "-", stdin=doc)
    assert r.code == 0 and r.first == "ok DEMO-0001 apply 6 seq=8", r.err
    d = cli.j(
        "apply",
        "--file",
        "-",
        stdin=batch({"op": "log", "text": "again"}, {"op": "task.skip", "task": "T1", "reason": "r"}),
    )
    assert d.data == {"count": 2, "events": ["log.added", "task.skipped"]}
    v = ws.view("1")
    assert v.tasks[0].state == "skipped" and v.acceptance[0].text == "it works" and v.questions[0].id == "Q1"


def test_apply_is_all_or_nothing(ws, cli):
    claimed(cli)
    n = len(ws.events("1"))
    for items, code, err in (
        ([{"op": "log", "text": "fine"}, {"op": "task.start", "task": "T9"}], 2, "not_found"),
        (
            [{"op": "log", "text": "fine"}, {"op": "section.set", "section": "findings", "message": "x"}],
            5,
            "invalid.input",
        ),
        ([{"op": "log", "text": "fine"}, {"op": "ac.add", "text": "bad \x07"}], 6, "parse.text"),
    ):
        r = cli.j("apply", "--file", "-", stdin=batch(*items))
        assert (r.code, r.err_code) == (code, err), items
        assert "item 2" in r.doc["error"]["message"]
        assert len(ws.events("1")) == n  # the first item was judged fine, and still nothing was written
    assert [e["type"] for e in ws.events("1")][-1] == "claim.taken"


def test_apply_base_rev_conflicts_refuse_the_whole_batch(ws, cli):
    claimed(cli)
    cli("section", "set", "plan", "-m", "v1")
    other = Cli(ws, session=OTHER)
    other("show", "1", "--section", "plan")
    other("section", "set", "plan", "-m", "other's", "--ref", "1")
    n = len(ws.events("1"))
    r = cli.j(
        "apply",
        "--file",
        "-",
        stdin=batch({"op": "log", "text": "x"}, {"op": "section.set", "section": "plan", "message": "mine"}),
    )
    assert (r.code, r.err_code) == (8, "conflict.section") and len(ws.events("1")) == n
    cli("show", "1", "--section", "plan")
    # two edits of one section in a batch: the second is based on the first
    r = cli.j(
        "apply",
        "--file",
        "-",
        stdin=batch(*[{"op": "section.set", "section": "plan", "message": f"v{i}"} for i in range(3)]),
    )
    assert r.code == 0 and "v2" in cli("show", "1", "--section", "plan").out


def test_apply_input_errors(ws, cli):
    claimed(cli)
    cases = [
        ("{not json", 6, "parse.json"),
        ('{"ops": [], "ops": []}', 6, "parse.json"),  # duplicate keys
        ('{"ops": 1}', 5, "invalid.input"),
        ("[]", 5, "invalid.input"),
        (batch(), 5, "invalid.input"),
        (batch({"op": "claim"}), 5, "invalid.input"),  # only the editing operations
        (batch({"op": "log", "text": "x", "ref": "2"}), 5, "invalid.input"),
        (batch({"op": "log", "file": "-"}), 5, "invalid.input"),
        (batch({"op": "log"}), 5, "invalid.input"),
        (batch({"op": "task.add", "text": "x", "proves": ["nope"]}), 5, "invalid.input"),
        (batch(*[{"op": "log", "text": "x"}] * 101), 5, "invalid.input"),
        (batch("x"), 5, "invalid.input"),
    ]
    for text, code, err in cases:
        r = cli.j("apply", "--file", "-", stdin=text)
        assert (r.code, r.err_code) == (code, err), text[:60]


def test_apply_needs_a_claim_for_task_items_and_supports_dry_run(ws, cli):
    claimed(cli)
    cli("task", "add", "x")
    cli("release")
    r = cli.j("apply", "--file", "-", stdin=batch({"op": "task.start", "task": "T1"}, ref="1"))
    assert (r.code, r.err_code) == (4, "claim.required")
    cli("claim", "1")
    n = len(ws.events("1"))
    r = cli("apply", "--file", "-", "--dry-run", stdin=batch({"op": "task.start", "task": "T1"}))
    assert r.code == 0 and "dry-run" in r.out and len(ws.events("1")) == n


def test_apply_retry_is_a_duplicate_not_a_second_batch(ws, cli):
    claimed(cli)
    doc = batch({"op": "log", "text": "once"}, {"op": "ac.add", "text": "a"})
    first = cli.j("apply", "--file", "-", stdin=doc)
    n = len(ws.events("1"))
    again = cli.j("apply", "--file", "-", stdin=doc)
    assert first.code == 0 and again.doc["duplicate"] is True and len(ws.events("1")) == n
