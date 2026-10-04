"""An artifact shown inline in gated text is part of what the human approved: gate hash v3 binds its sha256, so
replacing the image after approval invalidates the gate like a text edit. The verdict hash binds evidence images."""
import pytest

from orch.core import epics, store
from orch.core.gates import HASH_VERSION, gate_covers, gate_hash, gate_meta, gate_state, normalized_text


def _png(tmp_path, data=b"\x89PNG-one", name="login.png"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _t(ws, tid):
    return store.load(ws, tid)[1]


def _with_image(aops, ws, tmp_path):
    t = aops.new("Redesign login")
    aops.artifact_add(t.id, _png(tmp_path), label="Login mock-up")
    aops.set_section(t.id, "Requirements", "- The login looks like this:\n\n  ![Login mock-up](artifact:login.png)")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] Matches the mock-up")
    return t.id


def test_hash_version_three_equals_two_without_inline_artifacts(ws, aops):
    assert HASH_VERSION == 3
    t = aops.new("Plain")
    aops.set_section(t.id, "Requirements", "- r\n- see https://ex.com/x.png")
    t = _t(ws, t.id)
    assert gate_hash(t, "requirements", 3) == gate_hash(t, "requirements", 2)


def test_gate_hash_binds_the_inline_artifact(ws, aops, tmp_path):
    tid = _with_image(aops, ws, tmp_path)
    t = _t(ws, tid)
    sha = t.meta["artifacts"][0]["sha256"]
    assert ("artifact login.png", f"sha256:{sha}") in gate_meta(t, "requirements")
    assert f"artifact login.png: sha256:{sha}" in normalized_text(t, "requirements")
    assert "artifact login.png" in gate_covers("requirements", t)
    assert gate_hash(t, "requirements", 3) != gate_hash(t, "requirements", 2)


def test_replacing_an_approved_image_invalidates_the_gate(ws, aops, hops, tmp_path):
    tid = _with_image(aops, ws, tmp_path)
    hops.approve(tid, "requirements")
    t = _t(ws, tid)
    assert t.meta["gates"]["requirements"]["hash_v"] == 3 and gate_state(t, "requirements") == "approved"
    aops.artifact_add(tid, _png(tmp_path, b"\x89PNG-two"), replace=True)
    assert gate_state(_t(ws, tid), "requirements") == "invalidated"


def test_an_unlinked_reference_binds_missing_and_linking_it_later_invalidates(ws, aops, hops, tmp_path):
    t = aops.new("Ref first")
    aops.set_section(t.id, "Requirements", "![Mock](artifact:later.png)")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] ok")
    assert ("artifact later.png", "missing") in gate_meta(_t(ws, t.id), "requirements")
    hops.approve(t.id, "requirements")
    aops.artifact_add(t.id, _png(tmp_path, name="later.png"))
    assert gate_state(_t(ws, t.id), "requirements") == "invalidated"


def test_a_v2_approval_keeps_matching_its_own_hash(ws, aops, tmp_path):
    tid = _with_image(aops, ws, tmp_path)
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-01T09:00Z", "via": "tty",
                                       "hash": gate_hash(t, "requirements", 2), "hash_v": 2}
    store.save(ws, t, path)
    assert gate_state(_t(ws, tid), "requirements") == "approved"


def test_verdict_hash_binds_evidence_images(ws, aops, working, tmp_path):
    before = epics.verdict_hash([_t(ws, working)], ws)
    aops.artifact_add(working, _png(tmp_path), label="Jobs page shows 14 serverless jobs", ac=1, inline=True)
    shown = epics.verdict_hash([_t(ws, working)], ws)
    assert shown != before
    aops.artifact_add(working, _png(tmp_path, b"\x89PNG-two"), replace=True)
    assert epics.verdict_hash([_t(ws, working)], ws) != shown


# -- fix round 1: the binding and the renderer read the same Markdown tokens ------------------------------------------

FORMS = {
    "inline": "![Mock](artifact:login.png)",
    "reference": "![Mock][m]\n\n[m]: artifact:login.png",
    "angle brackets": "![Mock](<artifact:login.png>)",
    "entity": "![Mock](artifact:login&#46;png)",
    "backslash escape": "![Mock](artifact:login\\.png)",
    "percent encoding": "![Mock](artifact:logi%6E.png)",
    "artifact route": "![Mock](/a/{tid}/login.png)",
    "in a table": "| shot |\n|---|\n| ![Mock](artifact:login.png) |",
    "inside a link": "[![Mock](artifact:login.png)](https://ex.com)",
}


def _ticket_with(aops, ws, tmp_path, requirements):
    t = aops.new("Forms")
    aops.artifact_add(t.id, _png(tmp_path), label="Login mock-up")
    aops.set_section(t.id, "Requirements", requirements.format(tid=t.id))
    aops.set_section(t.id, "Acceptance criteria", "- [ ] Matches the mock-up")
    return t.id


@pytest.mark.parametrize("form", list(FORMS))
def test_every_form_that_renders_the_image_binds_it(ws, aops, hops, tmp_path, form):
    from orch.dashboard.markdown import artifact_scope, render_markdown
    tid = _ticket_with(aops, ws, tmp_path, FORMS[form])
    t = _t(ws, tid)
    sha = t.meta["artifacts"][0]["sha256"]
    assert ("artifact login.png", f"sha256:{sha}") in gate_meta(t, "requirements")
    assert 'alt="Mock"' in render_markdown(t.section("Requirements"), artifact_scope(t, ws))
    hops.approve(tid, "requirements")
    aops.artifact_add(tid, _png(tmp_path, b"\x89PNG-two"), replace=True)
    assert gate_state(_t(ws, tid), "requirements") == "invalidated"


@pytest.mark.parametrize("form", list(FORMS))
def test_every_form_binds_the_verdict_and_check_sees_it(ws, aops, working, tmp_path, form):
    from orch.core.check import run_checks
    aops.artifact_add(working, _png(tmp_path), label="Jobs page")
    before = epics.verdict_hash([_t(ws, working)], ws)
    text = FORMS[form].format(tid=working).replace("Mock", "Jobs page")
    aops.set_section(working, "Verification", "- AC1: the jobs page lists 14 jobs\n\n" + text)
    shown = epics.verdict_hash([_t(ws, working)], ws)
    aops.artifact_add(working, _png(tmp_path, b"\x89PNG-two"), replace=True)
    assert epics.verdict_hash([_t(ws, working)], ws) not in (shown,)
    gone = FORMS[form].format(tid=working).replace("logi%6E", "nope").replace("login", "nope")
    aops.set_section(working, "Verification", "- AC1: the jobs page lists 14 jobs\n\n" + gone)
    found = [f for f in run_checks(ws, emit_events=False) if f.ticket == working and f.code == "artifact-ref-missing"]
    assert found and "nope.png" in found[0].message
    assert before != shown


@pytest.mark.parametrize("text", ["![Mock](../artifacts/L-0999/login.png)", "![Mock](https://ex.com/login.png)",
                                  "`![Mock](artifact:login.png)`", "```\n![Mock](artifact:login.png)\n```"])
def test_what_does_not_render_the_image_binds_nothing(ws, aops, tmp_path, text):
    from orch.dashboard.markdown import artifact_scope, render_markdown
    tid = _ticket_with(aops, ws, tmp_path, text)
    t = _t(ws, tid)
    assert not [k for k, _ in gate_meta(t, "requirements") if k.startswith("artifact ")]
    assert "<img" not in render_markdown(t.section("Requirements"), artifact_scope(t, ws))


def test_the_renderer_shows_only_names_the_binding_returns(ws, aops, tmp_path, monkeypatch):
    from orch.core import artifacts
    from orch.dashboard.markdown import artifact_scope, render_markdown
    tid = _ticket_with(aops, ws, tmp_path, "![Mock](artifact:login.png)")
    t = _t(ws, tid)
    monkeypatch.setattr(artifacts, "refs_in_tokens", lambda tokens, ticket_id: [])
    assert "<img" not in render_markdown(t.section("Requirements"), artifact_scope(t, ws))


# -- fix round 2: links in route/path forms bind, or render inert; never a live unbound href ---------------------------

LINK_FORMS = {
    "route with fragment": "[a](/a/{tid}/login.png#f)",
    "percent-encoded ticket": "[a](/a/{tid_enc}/login.png)",
    "percent-encoded prefix": "[a](/%61/{tid}/login.png)",
    "route with query and fragment": "[a](/a/{tid}/login.png?v=aaaa#f)",
}


def _live_artifact_hrefs(html):
    import re
    return re.findall(r'href="([^"]*(?:/a/|artifacts/|%61/)[^"]*)"', html)


@pytest.mark.parametrize("form", list(LINK_FORMS))
def test_route_and_path_links_bind_and_carry_the_version(ws, aops, hops, tmp_path, form):
    from orch.dashboard.markdown import artifact_scope, render_markdown
    t = aops.new("Links")
    tid = t.id
    aops.artifact_add(tid, _png(tmp_path), label="Login mock-up")
    text = LINK_FORMS[form].format(tid=tid, tid_enc=tid.replace("-", "%2D"))
    aops.set_section(tid, "Requirements", text)
    aops.set_section(tid, "Acceptance criteria", "- [ ] ok")
    t = _t(ws, tid)
    sha = t.meta["artifacts"][0]["sha256"]
    assert ("artifact login.png", f"sha256:{sha}") in gate_meta(t, "requirements")
    hrefs = _live_artifact_hrefs(render_markdown(t.section("Requirements"), artifact_scope(t, ws)))
    assert hrefs == [f"/a/{tid}/login.png?v={sha[:16]}"]
    hops.approve(tid, "requirements")
    aops.artifact_add(tid, _png(tmp_path, b"\x89PNG-two"), replace=True)
    assert gate_state(_t(ws, tid), "requirements") == "invalidated"


@pytest.mark.parametrize("form", list(LINK_FORMS))
def test_unbound_route_and_path_links_render_inert(ws, aops, tmp_path, monkeypatch, form):
    from orch.core import artifacts
    from orch.dashboard.markdown import artifact_scope, render_markdown
    t = aops.new("Links")
    aops.artifact_add(t.id, _png(tmp_path), label="Login mock-up")
    text = LINK_FORMS[form].format(tid=t.id, tid_enc=t.id.replace("-", "%2D"))
    scope = artifact_scope(_t(ws, t.id), ws)
    gone = text.replace("login", "nope")
    html = render_markdown(gone, scope)
    assert _live_artifact_hrefs(html) == [] and "md-artifact-missing" in html
    monkeypatch.setattr(artifacts, "refs_in_tokens", lambda tokens, ticket_id: [])
    html = render_markdown(text, scope)
    assert _live_artifact_hrefs(html) == [] and "md-artifact-missing" in html


def test_a_bound_artifact_without_a_valid_hash_is_not_a_live_link(ws, aops, tmp_path):
    from orch.dashboard.markdown import artifact_scope, render_markdown
    t = aops.new("No hash")
    aops.artifact_add(t.id, _png(tmp_path))
    path, tk = store.load(ws, t.id)
    tk.meta["artifacts"][0]["sha256"] = "zz"
    store.save(ws, tk, path)
    html = render_markdown("[a](artifact:login.png) ![b](artifact:login.png)", artifact_scope(_t(ws, t.id), ws))
    assert _live_artifact_hrefs(html) == [] and "<img" not in html
