"""Widget blocks at write time (section set) and the agent helpers ``orch widgets check|types``."""

from __future__ import annotations

import json

GOOD = '```orch\n{"type":"callout","role":"info","text":"hello"}\n```'
BAD = '```orch\n{"type":"series","points":[[1,2]]}\n```'


def test_section_set_refuses_a_bad_block_and_stores_nothing(ws, cli):
    cli("new", "A ticket")
    cli("claim", "1")
    r = cli.j("section", "set", "context", "-m", BAD)
    assert r.err_code == "parse.text" and "widget block 1" in json.dumps(r.doc["error"])
    assert cli.j("section", "set", "plan", "-m", GOOD).err_code == "parse.text"
    assert cli.j("section", "set", "context", "-m", GOOD).code == 0


def test_widgets_check_and_types_cli(ws, cli, tmp_path):
    f = tmp_path / "b.md"
    f.write_text(GOOD)
    assert cli("widgets", "check", str(f)).code == 0
    f.write_text(BAD)
    r = cli.j("widgets", "check", str(f))
    assert r.err_code == "parse.text" and "points needs 2 to 500" in json.dumps(r.doc["error"])
    f.write_text('{"type":"callout","role":"info","text":"bare"}')  # a bare block needs no fence
    assert cli("widgets", "check", str(f)).code == 0
    assert cli("widgets", "check", str(f), "--section", "plan").code != 0
    out = cli("widgets", "types").out
    assert "timeline" in out and "example:" in out


def test_new_refuses_a_widget_block_in_the_summary(ws, cli):
    r = cli.j("new", "T", "--message", GOOD)
    assert r.err_code == "parse.text"
    assert cli("new", "T", "--message", "plain").code == 0


def test_handoff_refuses_a_bad_widget_block(ws, cli):
    cli("new", "A ticket")
    cli("claim", "1")
    assert cli.j("handoff", "-m", BAD).err_code == "parse.text"
    r = cli("handoff", "-m", "done\n\n" + GOOD)
    assert r.code == 0, r.out + r.err


def test_widgets_check_normalises_and_limits_like_section_set(ws, cli, tmp_path):
    f = tmp_path / "big.md"
    f.write_text("x" * 65537)
    assert cli.j("widgets", "check", str(f)).err_code in ("invalid.input", "parse.text")
    f.write_text("a\x00b")
    assert cli.j("widgets", "check", str(f)).err_code in ("invalid.input", "parse.text")
