"""Ticket widgets (docs/widgets.md): parsing, placement, digests, rendering, the CLI, the dashboard, AC projection."""
import hashlib
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core.model import Ticket
from orch.widgets import Ctx, check_ticket, parse_blocks, render_document, render_html, render_text, ticket_blocks
from orch.widgets.blocks import MAX_BLOCKS, load_strict
from orch.widgets.render import FRAME_CSP
from orch.widgets.types.checks import projection

F = "```"


def fence(obj, info="orch", run_=F) -> str:
    return f"{run_}{info}\n{obj if isinstance(obj, str) else json.dumps(obj)}\n{run_}"

def pin_t(ws, obj: dict) -> dict:
    """`obj` with the template pin `orch widget add` would write (registry.template_digest of the version now)."""
    from orch.widgets import registry
    _, current = registry.template_state(ws.home, obj["widget"], None)
    return {**obj, "sha256": current or "0" * 64}  # no such template: a well-formed pin, so "no template" shows



CHECKS = {"type": "checks", "source": "pytest -q", "rows": [{"ac": "AC1", "verdict": "met", "evidence": "suite green"},
                                                          {"ac": "AC2", "verdict": "not_met", "evidence": "404 on /x"}]}


# -- parsing ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, problem", [
    ('{"type": "text", "text": "a", "text": "b"}', "duplicate key"),
    ('{"type": "stats", "items": [{"label": "x", "value": NaN}]}', "NaN is not JSON"),
    ('{"type": "text", "text": Infinity}', "Infinity is not JSON"),
    ('[1, 2]', "not one JSON object"),
    ('{"a": ' * 9 + "1" + "}" * 9, "deeper than 8"),
    ('{"type": "text", "text": "' + "x" * 70000 + '"}', "larger than 64 KiB"),
    ('{"rows": [' + ",".join(["1"] * 501) + "]}", "501 items"),
    ('{"rows": [[' + ",".join(["1"] * 51) + "]]}", "inside an array"),
    ("{'type': 'text'}", "not strict JSON"),
])
def test_strict_json(raw, problem):
    data, error = load_strict(raw)
    assert data is None and problem in error


def test_fence_rules_and_line_numbers():
    text = "\n".join(["intro", fence({"type": "text", "text": "a"}), "",
                      f"````\n{fence({'type': 'text', 'text': 'inside a longer fence'})}\n````",
                      fence({"type": "text", "text": "tilde"}, run_="~~~~"),
                      fence({"type": "text", "text": "not orch"}, info="json"),
                      f"{F}orch\n{{\"type\": \"text\""])
    blocks = parse_blocks(text, "Context", offset=10)
    assert [b.line for b in blocks] == [12, 21, 27]
    assert [b.data["text"] for b in blocks[:2]] == ["a", "tilde"]
    assert blocks[2].error == "the fence is not closed"


def test_ticket_blocks_have_file_sections_lines_and_indexes():
    t = Ticket(meta={"id": "L-0001", "title": "x"},
               sections={"Context": fence({"type": "text", "text": "a"}), "Findings": "x\n\n" + fence({"type": "text", "text": "b"})})
    from orch.core.model import render_ticket
    raw = render_ticket(t)
    blocks = ticket_blocks(t, raw)
    lines = raw.split("\n")
    assert [(b.section, b.index) for b in blocks] == [("Context", 0), ("Findings", 1)]
    assert all(lines[b.line - 1].startswith("```orch") for b in blocks)


# -- validation and placement ------------------------------------------------------------------------------------

def _ticket(**sections):
    return Ticket(meta={"id": "L-0001", "title": "x"}, sections=sections)


def _codes(t, ws=None):
    return [(b.section, [p.code for p in b.problems]) for b in check_ticket(t, ws=ws)]


def test_placement_refused_in_gated_tasks_and_log_allowed_elsewhere():
    block = fence({"type": "text", "text": "a"})
    for name in ("Ask", "Summary", "Requirements", "Acceptance criteria", "Out of scope", "Plan", "Tasks", "Log"):
        assert _codes(_ticket(**{name: block})) == [(name, ["widget-place"])]
    for name in ("Context", "Current state", "Verification", "Findings", "Proposal", "Decisions", "Benchmarks"):
        assert _codes(_ticket(**{name: block})) == [(name, [])]


@pytest.mark.parametrize("obj, needle", [
    ({"text": "no layer"}, "exactly one of type, widget or html"),
    ({"type": "text", "widget": "x@1", "text": "a"}, "it names type, widget"),
    ({"type": "nope"}, "unknown core type"),
    ({"type": "text", "text": "a", "colour": "red"}, "Additional properties"),
    ({"type": "callout", "role": "pink", "text": "a"}, "'pink' is not one of"),
    ({"type": "text", "text": "a", "id": "Bad Id"}, "does not match"),
    ({"widget": "chart", "sha256": "0" * 64}, "does not match"),
    ({"widget": "chart@1", "sha256": "0" * 64}, "no template 'chart'"),
])
def test_schema_problems(obj, needle):
    [b] = check_ticket(_ticket(Context=fence(obj)))
    assert b.problems[0].code == "widget-schema" and needle in b.problems[0].message


def test_ids_unique_and_block_cap():
    twice = fence({"type": "text", "text": "a", "id": "x"})
    assert [p.code for b in check_ticket(_ticket(Context=f"{twice}\n\n{twice}")) for p in b.problems] == ["widget-schema"]
    many = "\n\n".join(fence({"type": "text", "text": str(i)}) for i in range(MAX_BLOCKS + 1))
    assert [p.message for p in check_ticket(_ticket(Context=many))[-1].problems] == [f"more than {MAX_BLOCKS} widgets in one ticket"]


def test_a_template_from_the_workspace_validates_its_data(ws):
    folder = ws.home / "widgets" / "gauge"
    folder.mkdir(parents=True)
    (folder / "widget.json").write_text(json.dumps({"name": "gauge", "title": "Gauge", "moment": "report", "versions": {
        "1": {"schema": {"type": "object", "additionalProperties": False, "required": ["v"],
                         "properties": {"v": {"type": "number"}}}}}}))
    ok, bad, missing = check_ticket(_ticket(Context="\n\n".join([fence(pin_t(ws, {"widget": "gauge@1", "data": {"v": 3}})),
                                                                 fence(pin_t(ws, {"widget": "gauge@1", "data": {"v": "x"}})),
                                                                 fence(pin_t(ws, {"widget": "gauge@2"}))])), ws=ws)
    assert ok.problems == [] and "is not of type 'number'" in bad.problems[0].message
    assert "no version 2" in missing.problems[0].message


def test_gate_hash_unchanged_by_a_verification_widget(aops):
    from orch.core.gates import gate_hash
    t = aops.new("Thing")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    aops.set_section(t.id, "Plan", "1. do")
    from orch.core import store
    before = store.load(aops.ws, t.id)[1]
    aops.append_section(t.id, "Verification", fence(CHECKS))
    after = store.load(aops.ws, t.id)[1]
    assert "```orch" in after.section("Verification")
    assert all(gate_hash(before, g) == gate_hash(after, g) for g in ("requirements", "plan"))


# -- digests -----------------------------------------------------------------------------------------------------

def test_digest_states(ws):
    folder = ws.artifacts_dir / "L-0001"
    folder.mkdir(parents=True)
    (folder / "page.html").write_text("<p>hi</p>")
    (ws.artifacts_dir / "L-0002").mkdir()
    (ws.artifacts_dir / "L-0002" / "other.html").write_text("x")
    good = hashlib.sha256(b"<p>hi</p>").hexdigest()
    blocks = check_ticket(_ticket(Findings="\n\n".join([
        fence({"html": "artifacts/L-0001/page.html", "sha256": good}),
        fence({"html": "artifacts/L-0001/page.html", "sha256": "0" * 64}),
        fence({"html": "artifacts/L-0001/gone.html", "sha256": good}),
        fence({"html": "artifacts/L-0002/other.html", "sha256": good}),
        fence({"html": "artifacts/L-0001/../L-0002/other.html", "sha256": good}),
    ])), ws=ws)
    states = [[(p.code, p.level) for p in b.problems] for b in blocks]
    # a changed one-off page is an error (it never runs); a changed image inside a block is a warning (not shown)
    assert states == [[], [("widget-digest", "error")], [("widget-digest", "error")], [("widget-digest", "error")],
                      [("widget-digest", "error")]]
    ctx = Ctx(ticket=_ticket(), ws=ws)
    changed, missing = str(render_html(blocks[1], ctx)), str(render_html(blocks[2], ctx))
    assert "changed since this widget was written" in changed and "w-frame" not in changed  # no frame: it never runs
    assert "is missing" in missing and "w-frame" not in missing


# -- rendering ---------------------------------------------------------------------------------------------------

def test_core_types_render_with_chrome_and_text():
    from orch.widgets import registry
    assert {"text", "callout", "stats", "table", "chips", "links", "checks"} <= set(registry.core_types())
    for mod in registry.core_types().values():
        [b] = check_ticket(_ticket(Context=fence({**mod.EXAMPLE, "id": "ex", "title": "Example"})))
        assert b.problems == [], mod.NAME
        html = str(render_html(b, Ctx(ticket=_ticket())))
        assert html.startswith('<figure class="w w-t-') and 'id="w-ex"' in html and 'data-layer="core"' in html
        assert '<details class="w-alt">' in html and "<span class=\"w-chip\">core</span>" in html
        assert render_text(b, Ctx()).startswith("[Example · core]")


def test_unsafe_link_stays_text_and_markup_is_escaped():
    [b] = check_ticket(_ticket(Context=fence({"type": "links", "title": "<b>x</b>", "items": [
        {"label": "go", "url": "javascript:alert(1)"}, {"label": "ok", "url": "https://e.test/"}]})))
    html = str(render_html(b, Ctx()))
    assert 'href="javascript' not in html and "&lt;b&gt;x&lt;/b&gt;" in html and 'href="https://e.test/"' in html


def test_agent_layers_with_html_on_and_off(ws, monkeypatch):
    import re
    folder = ws.artifacts_dir / "L-0001"
    folder.mkdir(parents=True)
    (folder / "p.html").write_text("<p id=x>x</p>")
    block = {"html": "artifacts/L-0001/p.html", "sha256": hashlib.sha256(b"<p id=x>x</p>").hexdigest(),
             "caption": "Replay of run 3"}
    t = _ticket(Findings=fence(block))
    [b] = check_ticket(t, ws=ws)
    on = str(render_html(b, Ctx(ticket=t, ws=ws, html=True)))
    m = re.search(rf'class="w-frame" data-doc-url="/w/L-0001/Findings/{b.digest}\?n=([\w-]+)" data-nonce="([\w-]+)"', on)
    assert m and m.group(1) == m.group(2) and 'data-min-height="160"' in on
    assert "renderer not installed" not in on and "Replay of run 3" in on and "agent HTML · one-off" in on
    assert '<details class="w-alt">' in on  # the frame draws: the text waits behind the toggle
    off = str(render_html(b, Ctx(ticket=t, ws=ws, html=False)))
    assert "Agent HTML is off" in off and "w-frame" not in off and '<details class="w-alt" open>' in off
    doc = render_document(b, Ctx(ticket=t, ws=ws, html=True, nonce="abcdefgh12"))
    assert '<meta name="orch-frame" content="abcdefgh12">' in doc and "<p id=x>x</p>" in doc and "window.orch" in doc
    body = doc[doc.index("<body"):]  # agent HTML never carries the chrome, with or without chrome=False
    assert "<figcaption" not in body and "w-alt" not in body and "agent HTML · one-off" not in body
    bare = render_document(b, Ctx(ticket=t, ws=ws, html=True, nonce="abcdefgh12"), chrome=False)
    assert bare[bare.index("<body"):] == body
    doc_off = render_document(b, Ctx(ticket=t, ws=ws, html=False))
    assert "<script" not in doc_off and "Replay of run 3" in doc_off and 'class="w-frame"' not in doc_off
    from orch.widgets import frames  # a hook without a renderer still says so
    monkeypatch.setattr(frames, "INSTALLED", False)
    assert "agent HTML renderer not installed" in str(render_html(b, Ctx(ticket=t, ws=ws, html=True)))


def test_render_document_is_self_contained():
    [b] = check_ticket(_ticket(Context=fence(CHECKS)))
    doc = render_document(b, Ctx(theme="dark"))
    assert doc.startswith("<!doctype html>") and FRAME_CSP in doc and 'data-theme="dark"' in doc
    assert "--ok-bg" in doc and ".w-verdict" in doc and "<script" not in doc and "<link" not in doc


def test_render_document_without_chrome_keeps_only_the_body():
    block = {**CHECKS, "title": "Verdicts", "source": "pytest -q", "caption": "Run 3"}
    [b] = check_ticket(_ticket(Context=fence(block)))
    full = render_document(b, Ctx())
    bare = render_document(b, Ctx(), chrome=False)
    body = bare[bare.index("<body"):]
    assert "<figcaption" in full and "Source: pytest -q" in full and "<details" in full
    assert "<figcaption" not in body and "<details" not in body and "Source:" not in body and "Verdicts" not in body
    assert 'class="w-verdict' in body and "Run 3" in body and FRAME_CSP in bare


def test_frame_documents_carry_the_dashboard_fonts_inline():
    from orch.widgets.assemble import read_static
    from orch.widgets.render import STATIC, frame_tokens
    fonts = sum(p.stat().st_size for p in (STATIC / "fonts").glob("*.woff2"))
    assert fonts <= 120 * 1024  # the budget for inlining them at all
    [b] = check_ticket(_ticket(Context=fence(CHECKS)))
    for css in (render_document(b, Ctx()), read_static()[1], frame_tokens()):
        assert css.count("@font-face") == 2 and 'font-family:"Figtree"' in css and 'font-family:"Manrope"' in css
        assert "url(data:font/woff2;base64," in css and "/static/fonts" not in css


def test_invalid_and_misplaced_render_as_code_with_where():
    t = _ticket(Plan=fence({"type": "text", "text": "a"}), Findings=fence('{"type": "text",}'))
    misplaced, invalid = check_ticket(t)
    m, i = str(render_html(misplaced, Ctx(ticket=t))), str(render_html(invalid, Ctx(ticket=t)))
    assert "Shown as code" in m and "Plan, line" in m and "<pre class=\"w-raw\">" in m
    assert "Widget not shown" in i and f"Findings, line {invalid.line}" in i and "not strict JSON" in i


def test_ac_projection_takes_the_latest_verdict():
    later = {"type": "checks", "rows": [{"ac": "AC2", "verdict": "met", "evidence": "fixed"}]}
    t = _ticket(**{"Acceptance criteria": "- [ ] a\n- [ ] b",
                   "Verification": "\n\n".join([fence(CHECKS), fence(later), fence({"type": "checks", "rows": []})])})
    got = projection(t)
    assert {n: v["verdict"] for n, v in got.items()} == {1: "met", 2: "met"} and got[2]["word"] == "Met"


# -- CLI ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, ws_root, capsys):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setattr(actor, "is_interactive", lambda: False)

    def call(*args):
        capsys.readouterr()
        code = run(list(args))
        out = capsys.readouterr()
        return code, out.out, out.err
    return call


def test_cli_add_check_render(cli, aops, ws, tmp_path):
    t = aops.new("Speed up the board")
    code, out, err = cli("widget", "add", t.id, "--section", "Verification", "--type", "checks",
                         "--data", json.dumps({"rows": CHECKS["rows"]}), "--source", "pytest -q", "--id", "acs", "--json")
    assert code == 0, err
    assert json.loads(out)["index"] == 0 and "AC1: Met" in json.loads(out)["text"]
    assert cli("widget", "add", t.id, "--section", "Plan", "--type", "text", "--data", '{"text": "x"}')[0] != 0
    assert cli("widget", "add", t.id, "--section", "Findings", "--type", "text", "--data", '{"txt": "x"}')[0] != 0
    assert cli("widget", "add", t.id, "--section", "Findings", "--type", "text", "--data", '{"text": "x"}',
               "--id", "acs")[0] != 0  # id taken
    # a one-off page: the digest is filled in from the artifact
    page = tmp_path / "replay.html"
    page.write_text("<p>r</p>")
    aops.artifact_add(t.id, page)
    code, out, err = cli("widget", "add", t.id, "--section", "Findings", "--html", "replay.html", "--json")
    assert code == 0, err
    assert json.loads(out)["block"]["sha256"] == hashlib.sha256(b"<p>r</p>").hexdigest()
    assert cli("widget", "check", t.id)[1].strip() == "all good"
    code, out, _ = cli("widget", "render", t.id, "--text", "--id", "acs")
    assert code == 0 and "AC2: Not met" in out
    code, out, _ = cli("widget", "render", t.id, "--html", "--id", "acs")
    assert out.startswith("<!doctype html>") and FRAME_CSP in out
    code, out, _ = cli("show", t.id, "--widgets")
    assert out.index("AC2: Not met — 404 on /x") < out.index("## Log")
    # a hand-edited bad block: `orch widget check` and `orch check` both report it
    from orch.core import store
    path = store.resolve(ws, t.id).path
    path.write_text(path.read_text().replace('"verdict": "met"', '"verdict": "great"'))
    code, out, _ = cli("widget", "check", "--json")
    assert code == 5 and json.loads(out)[0]["code"] == "widget-schema"
    code, out, _ = cli("check", "--json")
    assert any(f["code"] == "widget-schema" and f["ticket"] == t.id for f in json.loads(out))


def test_cli_types_show_list(cli):
    code, out, _ = cli("widget", "types", "--json")
    assert code == 0 and {"checks", "text", "mermaid"} <= {r["name"] for r in json.loads(out)}
    code, out, _ = cli("widget", "show", "callout", "--json")
    assert json.loads(out)["schema"]["properties"]["role"]["enum"] == ["ok", "info", "warn", "err", "neu"]
    rows = json.loads(cli("widget", "list", "--json")[1])
    assert {r["name"] for r in rows} >= {"mermaid", "before-after"} and all(r["uses"] == 0 for r in rows)
    code, out, _ = cli("widget", "show", "mermaid@1", "--json")
    shown = json.loads(out)
    assert code == 0 and list(shown["versions"]) == ["1"] and "source" in shown["example"]
    assert shown["body"].endswith("v1.html")
    code, _, err = cli("widget", "show", "mermaid@9")
    assert code != 0 and "no version 9" in err


# -- config ------------------------------------------------------------------------------------------------------

def test_widgets_html_is_off_by_default_and_the_guard_refuses_an_agent_edit(ws, configure):
    from orch.hooks.guard import evaluate
    assert Ctx.of(ws).html is False
    assert Ctx.of(configure(widgets={"html": False})).html is False
    path = str(ws.home / "config.json")
    text = (ws.home / "config.json").read_text()
    off = json.dumps({**json.loads(text), "widgets": {"html": True}})
    denied = evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": path, "content": off}})
    assert not denied.allow and "human's setting" in denied.reason
    other = json.dumps({**json.loads(text), "customer": "acme2"})
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": path, "content": other}}).allow
    bash = evaluate(ws, {"tool_name": "Bash", "tool_input": {
        "command": "jq '.widgets.html=false' orchestrator/config.json > c && mv c orchestrator/config.json"}})
    assert not bash.allow


# -- dashboard ---------------------------------------------------------------------------------------------------

def test_ticket_page_draws_widgets_errors_and_verdict_chips(dash, put):
    tid = put("in-progress", title="Widgets", sections={
        "Acceptance criteria": "- [ ] loads fast\n- [ ] no 404",
        "Verification": "- AC1: ran the timing suite twice\n\n" + fence({**CHECKS, "id": "acs"}),
        "Findings": fence({"type": "stats", "items": [{"label": "p95", "value": "41 ms"}]}) + "\n\n" + fence('{"type": 1'),
        "Plan": fence({"type": "text", "text": "plan widget"})})
    body = dash.get(f"/t/{tid}").text
    assert 'href="/static/widgets/core.css' in body
    assert 'id="w-acs"' in body and "w-stat-v" in body
    assert "Widget not shown" in body and "Findings, line" in body
    assert "Shown as code" in body
    assert body.count("the agent&#39;s check") + body.count("the agent's check") == 2 and "Not met" in body


def test_widget_document_route(dash, put, wurl):
    tid = put("in-progress", sections={"Findings": fence({**CHECKS, "id": "acs"})})
    r = dash.get(wurl(tid, "acs"))
    assert r.status_code == 200 and r.text.startswith("<!doctype html>")
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts;") and "frame-ancestors 'self'" in csp and "connect-src 'none'" in csp
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert dash.get(wurl(tid, "0")).status_code == 200
    for old in (f"/w/{tid}/0", f"/w/{tid}/acs", f"/w/{tid}/Findings/9", f"/w/{tid}/Findings/{'0' * 64}",
                wurl(tid, "acs").replace("/Findings/", "/Context/")):
        assert dash.get(old).status_code == 404, old  # no id or index lookup, no other section, no other digest


def test_checks_stack_into_rows_when_narrow():
    """At a phone's width the checks table is a row per criterion, never wider than its box (drawn and checked
    at 390 px in orchestrator tests/browser/test_widgets.py::test_end_to_end_documents_draw_in_the_pwa)."""
    import re
    from orch.widgets.render import widget_css
    narrow = re.search(r"@media \(max-width: 560px\) \{(.*?)\n\}", widget_css(), re.S)
    assert narrow and ".w-checks tr { display: grid;" in narrow.group(1) and "overflow-wrap: anywhere" in narrow.group(1)


def test_heading_split_is_linear_and_keeps_its_titles():
    import time
    from orch.core.model import h2_title, section_text_problem
    from orch.widgets import parse_blocks
    assert [h2_title(x) for x in ("## Findings", "## Findings ##", "## a #  ", "## ###", "##    ", "#  x", "## x\t")] \
        == ["Findings", "Findings", "a", "#", "", None, "x"]
    for line in ("## " + " " * 10_000 + "x", "## " + " " * 10_000, "## " + "# " * 5_000 + "x"):
        start = time.perf_counter()
        h2_title(line)
        section_text_problem(line)
        parse_blocks(line + "\n```orch\n{}\n```", "Findings")
        assert time.perf_counter() - start < 0.05, line[:10]
