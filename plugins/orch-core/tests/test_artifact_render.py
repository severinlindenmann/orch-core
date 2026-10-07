"""Show, not tell: ticket artifacts render inline (thumbnail, click for full size) wherever a section is shown; remote
images never load; the ticket page groups artifacts by kind; cards count them; a swapped file is not served as the
linked one."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import store  # noqa: E402
from orch.dashboard.markdown import artifact_scope, render_markdown  # noqa: E402


def _png(tmp_path, name="shot.png", data=b"\x89PNG-one"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _t(ws, tid):
    return store.load(ws, tid)[1]


# -- renderer ------------------------------------------------------------------------------------------------------

def test_an_artifact_image_renders_as_a_lazy_thumbnail_linking_the_full_file(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    t = _t(ws, working)
    sha = t.meta["artifacts"][0]["sha256"]
    html = render_markdown('![Jobs <b>"page"</b>](artifact:shot.png)', artifact_scope(t, ws))
    url = f"/a/{working}/shot.png?v={sha[:16]}"
    assert f'<a class="md-artifact" href="{url}" target="_blank" rel="noopener">' in html
    assert f'<img src="{url}" alt="Jobs &lt;b&gt;&quot;page&quot;&lt;/b&gt;" loading="lazy" decoding="async">' in html
    assert "<b>" not in html


def test_svg_is_only_an_img_and_other_files_are_links(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path, "flow.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
    aops.artifact_add(working, _png(tmp_path, "cost.csv", b"a,b"))
    scope = artifact_scope(_t(ws, working), ws)
    html = render_markdown("![Flow](artifact:flow.svg) ![Cost](artifact:cost.csv) [sheet](artifact:cost.csv)", scope)
    assert '<img src="/a/' in html and "<svg" not in html
    assert html.count("<img") == 1
    assert re.search(r'<a class="md-artifact" href="/a/[^"]+/cost\.csv\?v=[0-9a-f]{16}"[^>]*>Cost</a>', html)
    assert re.search(r'<a href="/a/[^"]+/cost\.csv\?v=[0-9a-f]{16}"[^>]*>sheet</a>', html)


@pytest.mark.parametrize("src", ["https://evil.example.com/pixel.png", "http://x.example.com/a.svg",
                                 "//cdn.example.com/a.png", "javascript:alert(1)", "data:image/png;base64,AAAA",
                                 "/a/L-0099/shot.png", "../artifacts/L-0099/shot.png"])  # another ticket's paths
def test_remote_and_other_images_are_never_loaded(ws, aops, working, tmp_path, src):
    aops.artifact_add(working, _png(tmp_path))
    html = render_markdown(f"![pic]({src})", artifact_scope(_t(ws, working), ws))
    assert "<img" not in html and 'href="javascript' not in html and 'href="data' not in html
    if src.startswith("http"):
        assert f'<a href="{src}"' in html and "pic" in html


def test_unlinked_or_unknown_artifacts_show_a_note_not_an_image(ws, aops, working, tmp_path):
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    (d / "loose.png").write_bytes(b"x")  # on disk, not linked
    html = render_markdown("![Loose](artifact:loose.png) ![Gone](artifact:../x.png)", artifact_scope(_t(ws, working), ws))
    assert "<img" not in html and "not linked" in html


def test_without_a_ticket_artifact_images_stay_text(ws):
    assert "<img" not in render_markdown("![Login](artifact:shot.png)")


def test_markdown_tables_render_as_tables():
    html = render_markdown("| Meter | Before | After |\n|---|---|---|\n| A | 3 | 0 |")
    assert "<table>" in html and "<th>Before</th>" in html and "<td>0</td>" in html


# -- the artifact route --------------------------------------------------------------------------------------------

def test_a_swapped_file_is_not_served_as_the_linked_one(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    sha = _t(ws, working).meta["artifacts"][0]["sha256"]
    assert dash.get(f"/a/{working}/shot.png?v={sha[:16]}").status_code == 200
    (ws.artifacts_dir / working / "shot.png").write_bytes(b"swapped")
    r = dash.get(f"/a/{working}/shot.png?v={sha[:16]}")
    assert r.status_code == 409 and "changed" in r.text


# -- ticket page ---------------------------------------------------------------------------------------------------

def test_ticket_page_shows_images_inline_in_agreed_and_proven(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path, "mock.png"), label="Mock-up")
    aops.set_section(working, "Requirements", "- Like this:\n\n  ![Mock-up of the jobs page](artifact:mock.png)")
    aops.artifact_add(working, _png(tmp_path), label="Jobs page shows 14 serverless jobs", ac=1, inline=True)
    html = dash.get(f"/t/{working}?open=all").text
    assert 'alt="Mock-up of the jobs page"' in html
    proven = html[html.index('id="proven"'):]
    assert 'alt="Jobs page shows 14 serverless jobs"' in proven


def test_ticket_page_groups_artifacts_by_kind_with_their_task_and_criterion(dash, ws, aops, working, tmp_path):
    _, ids = aops.task_add(working, [{"text": "the work"}])
    aops.artifact_add(working, _png(tmp_path), label="Jobs page", ac=1)
    aops.artifact_link(working, "https://ci.example.com/run/5", label="CI run", kind="build", task=ids[0])
    d = ws.artifacts_dir / working
    (d / "loose.log").write_text("x", encoding="utf-8")
    html = dash.get(f"/t/{working}?open=all").text
    area = html[html.index('id="ticket-artifacts"'):]
    area = area[:area.index('<section class="card act-card"')]
    assert "Screenshots" in area and "Builds" in area  # M: the aside panel groups by orch's kinds
    assert 'loading="lazy"' in area and "Jobs page" in area and "AC1" in area
    assert '<a class="file-open" href="https://ci.example.com/run/5" target="_blank" rel="noopener noreferrer">' in area
    assert '<span class="artifact-url">https://ci.example.com/run/5</span>' in area  # the full address, as text
    assert ids[0] in area
    assert "loose.log" in area and "not linked" in area


def test_hidden_characters_in_a_hand_edited_label_are_shown(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    path, t = store.load(ws, working)
    t.meta["artifacts"][0]["label"] = "safe‮gnp.exe"
    store.save(ws, t, path)
    html = dash.get(f"/t/{working}?open=all").text
    assert "‮" not in html and "U+202E" in html


def test_board_card_counts_artifacts(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    aops.artifact_link(working, "https://ci.example.com/run/5")
    html = dash.get("/board").text
    assert "2 artifacts" in html


def test_today_approve_card_shows_the_image_and_counts_artifacts(dash, ws, aops, tmp_path):
    t = aops.new("Redesign login")
    aops.artifact_add(t.id, _png(tmp_path, "mock.png"), label="Mock-up")
    aops.set_section(t.id, "Requirements", "- The login looks like this:\n\n  ![Login mock-up](artifact:mock.png)")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] Matches the mock-up")
    html = dash.get("/groom?scope=backlog").text  # backlog approvals are grooming: the one-by-one view
    card = html[html.index(f'data-ticket="{t.id}"'):]
    assert 'alt="Login mock-up"' in card and 'loading="lazy"' in card
    assert "1 artifact<" in card


def test_verdict_card_shows_evidence_images(dash, ws, aops, hops, working, tmp_path, close_tasks):
    aops.set_section(working, "Plan", "1. migrate")
    hops.approve(working, "plan")
    close_tasks(aops, working)
    aops.artifact_add(working, _png(tmp_path), label="Jobs page shows 14 serverless jobs", ac=1, inline=True)
    aops.set_section(working, "Verification", _t(ws, working).section("Verification") + "\n- AC2: cost compared, 12% lower")
    aops.move(working, "testing")
    html = dash.get("/").text
    assert 'alt="Jobs page shows 14 serverless jobs"' in html


def test_artifacts_are_not_served_from_a_symlinked_artifacts_folder(dash, ws, put, tmp_path):
    import shutil
    tid = put("in-progress")
    outside = tmp_path / "elsewhere" / tid
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    shutil.rmtree(ws.artifacts_dir)
    ws.artifacts_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert dash.get(f"/a/{tid}/secret.txt").status_code == 404
