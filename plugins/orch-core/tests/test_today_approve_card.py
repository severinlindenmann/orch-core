"""#20: an approval card on Today shows the full text its hash binds, and the server refuses any other hash."""
import html as html_mod
import re

import pytest

pytest.importorskip("fastapi")

LONG = "\n".join(f"- requirement line {i}" for i in range(1, 12))


def _card(page: str, tid: str) -> str:
    """The decision card of `tid` on Today, or on the one-by-one groom view for an unclaimed backlog ticket."""
    for chunk in page.split('<article class="decision-card decision')[1:]:
        if f'href="/t/{tid}"' in chunk:
            return chunk.split("</article>", 1)[0]
    raise AssertionError(f"no card for {tid}")


def _rendered_parts(card: str) -> list[str]:
    return [html_mod.unescape(n) for n in re.findall(r'data-gate-part="([^"]+)"', card)]


def _seen(card: str) -> str:
    return re.search(r'name="seen" value="([^"]+)"', card).group(1)


def test_requirements_card_renders_every_hashed_part_and_posts_that_hash(dash, ws, put):
    from orch.core import store
    from orch.core.gates import gate_covers, gate_hash
    tid = put("backlog", sections={"Requirements": LONG, "Acceptance criteria": "- [ ] AC one\n- [ ] AC two",
                                   "Out of scope": "Restores are out of scope."})
    t = store.load(ws, tid)[1]
    card = _card(dash.get("/groom").text, tid)
    assert _rendered_parts(card) == gate_covers("requirements", t)
    assert _seen(card) == gate_hash(t, "requirements")
    for i in range(1, 12):
        assert f"requirement line {i}" in card  # no excerpt cut: every line is shown
    assert "AC two" in card and "Restores are out of scope." in card
    assert re.search(r'data-gate-part="size">[^<]*size: <b>m</b>', card)
    assert re.search(r'data-gate-part="type">[^<]*type: <b>feature</b>', card)


def test_summary_is_rendered_when_the_hash_covers_it(dash, ws, put):
    from orch.core import store
    from orch.core.gates import gate_covers
    tid = put("backlog", sections={"Summary": "- backs up nightly", "Requirements": "r", "Acceptance criteria": "- a"})
    card = _card(dash.get("/groom").text, tid)
    assert _rendered_parts(card) == gate_covers("requirements", store.load(ws, tid)[1])
    assert _rendered_parts(card)[0] == "Summary" and "backs up nightly" in card


def test_plan_card_renders_the_full_plan(dash, ws, working, aops):
    from orch.core import store
    from orch.core.gates import gate_hash
    aops.set_section(working, "Plan", LONG)
    card = _card(dash.get("/").text, working)
    assert _rendered_parts(card) == ["Plan"]
    assert "requirement line 11" in card and _seen(card) == gate_hash(store.load(ws, working)[1], "plan")


def test_re_approve_card_renders_the_full_text(dash, ws, working, aops, hops):
    aops.set_section(working, "Plan", "1. first")
    hops.approve(working, "plan")
    aops.set_section(working, "Plan", LONG)
    card = _card(dash.get("/").text, working)
    assert "Re-approve" in card and _rendered_parts(card) == ["Plan"] and "requirement line 11" in card


def test_approve_form_confirms_with_the_hash_prefix(dash, ws, put):
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- a"})
    card = _card(dash.get("/groom").text, tid)
    form = card.split(f'action="/t/{tid}/approve"', 1)[1].split("</form>", 1)[0]
    seen = _seen(card)
    # the inline two-step confirm names the hash it binds (no browser popup)
    assert f"data-inline-confirm=\"Confirm · requirements {seen[7:11]}" in card.split(f'action="/t/{tid}/approve"', 1)[1].split(">", 1)[0]
    assert seen[7:15] in card and 'name="seen"' in form


def test_server_refuses_a_hash_of_less_than_the_full_gated_text(dash, ws, put):
    """A hash over the sections only (v1, without size and type) is not the text the card showed."""
    from orch.core import gates, store
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- a"})
    t = store.load(ws, tid)[1]
    for wrong in (gates.gate_hash(t, "requirements", version=1), "sha256:" + "0" * 64):
        r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": wrong}, follow_redirects=False)
        assert r.status_code == 303 and "changed+since+you+opened+it" in r.headers["location"]
    assert gates.gate_state(store.load(ws, tid)[1], "requirements") == "pending"
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": gates.gate_hash(t, "requirements")},
                  follow_redirects=False)
    assert gates.gate_state(store.load(ws, tid)[1], "requirements") == "approved"


def test_size_change_after_render_is_refused(dash, ws, put):
    from orch.core import gates, store
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- a"})
    seen = _seen(_card(dash.get("/groom").text, tid))
    path, t = store.load(ws, tid)
    t.meta["size"] = "xs"
    store.save(ws, t, path)
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen}, follow_redirects=False)
    assert "changed+since+you+opened+it" in r.headers["location"]
    assert gates.gate_state(store.load(ws, tid)[1], "requirements") == "pending"


def test_link_reference_definitions_are_shown_not_hidden(dash, ws, put):
    """`[x]: url` renders as nothing in Markdown but is part of the hash: the card shows the raw text."""
    tid = put("backlog", sections={"Requirements": "Use the [runbook].\n\n[runbook]: https://evil.test/run SECRET-NOTE",
                                   "Acceptance criteria": "- a"})
    card = _card(dash.get("/groom").text, tid)
    assert "SECRET-NOTE" in card and "https://evil.test/run" in card and 'class="gate-raw"' in card


def test_cli_rejects_wrong_case_and_underscore_spellings(ws, put, monkeypatch, capsys):
    from orch import actor
    from orch.cli import run
    from orch.core import store
    tid = put("testing", sections={"Verification": "ok"})
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": tid)
    assert run(["move", tid, "DONE"]) == 2
    assert run(["request_changes", tid, "plan", "-m", "x"]) == 2
    assert run(["verdict", tid, "DONE"]) == 2
    assert store.load(ws, tid)[1].status == "testing"


def test_requirements_a_refine_agent_waits_on_are_a_card_on_today(dash, ws, put):
    """A claimed backlog ticket: the refine agent waits, so its requirements card is on Today with the full text."""
    from orch.core import store
    from orch.core.gates import gate_covers
    tid = put("backlog", claim={"harness": "claude-code", "session": "s1", "at": "2026-10-04T08:00Z"},
              sections={"Requirements": LONG, "Acceptance criteria": "- [ ] AC one"})
    card = _card(dash.get("/").text, tid)
    assert _rendered_parts(card) == gate_covers("requirements", store.load(ws, tid)[1])


def test_groom_view_steps_through_the_backlog(dash, put):
    a = put("backlog", title="First", sections={"Requirements": "r", "Acceptance criteria": "- a"})
    b = put("backlog", title="Second", sections={"Requirements": "r", "Acceptance criteria": "- a"})
    html = dash.get("/groom").text
    nxt = f"/groom?scope=backlog&amp;at={b}-approve-requirements"
    assert "1 of 2" in html and f'href="{nxt}" rel="next"' in html and 'rel="prev"' not in html
    assert f'name="next" value="{nxt}"' in html  # after the approval the next one is shown
    html = dash.get(f"/groom?at={b}").text  # a ticket id works as well as a card id
    assert "2 of 2" in html and f'href="/groom?scope=backlog&amp;at={a}-approve-requirements" rel="prev"' in html
    assert 'rel="next"' not in html
