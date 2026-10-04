import os

import pytest

from addon_fixtures import loaded
from orch.addons.manifest import MENU_ICONS, parse_manifest
from orch.addons.widgets import QR, widget_problems
from orch.core.events import read_events
from orch.errors import ValidationError
from addon_fixtures import GOOD


def test_addon_adds_an_artifact_as_its_agent(ws, aops):
    t = aops.new("Artifacts")
    la = loaded(ws, object(), name="tixlike")
    src = la.ctx.state_dir / "in" / "phone.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"jpg")
    dest = la.ctx.ops().add_artifact(t.id, src, "remote-phone.jpg", context=True)
    assert dest.read_bytes() == b"jpg"
    e = read_events(ws, t.id)[-1]
    assert e.kind == "artifact.added" and e.actor == "agent:addon:tixlike" and e.data == {"name": "remote-phone.jpg", "kind": "screenshot",
                                                                                         "context": True}


def test_add_artifact_refuses_files_outside_state_and_symlinks(ws, aops, tmp_path):
    t = aops.new("Artifacts")
    la = loaded(ws, object(), name="tixlike")
    outside = tmp_path / "x.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        la.ctx.ops().add_artifact(t.id, outside)
    link = la.ctx.state_dir / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)
    with pytest.raises(ValidationError):
        la.ctx.ops().add_artifact(t.id, link)


def test_cli_context_flag(ws, aops, tmp_path, capsys):
    from orch.cli import run
    t = aops.new("Ctx")
    f = ws.root / "notes.md"
    f.write_text("n", encoding="utf-8")
    assert run(["artifact", "add", t.id, str(f), "--context"]) == 0
    assert read_events(ws, t.id)[-1].data["context"] is True


def test_qr_widget_checks_and_renders_svg(ws):
    m = parse_manifest(GOOD)
    assert widget_problems(QR("https://example.invalid/pair#abc", "Scan with the phone"), slot="page.x", manifest=m) == []
    assert widget_problems(QR("x" * 1001), slot="page.x", manifest=m)
    from orch.dashboard.qr import qr_matrix
    rows = qr_matrix("https://example.invalid/pair#abc")
    assert len(rows) >= 21 and all(len(r) == len(rows) for r in rows)
    assert rows[0][:7] == [True] * 7                       # finder pattern


def test_share_icon_and_remote_humans_key():
    assert "share" in MENU_ICONS
    m = parse_manifest({**GOOD, "capabilities": [*GOOD["capabilities"], "decisions"], "remote_humans": True})
    assert m.remote_humans is True
    assert parse_manifest(GOOD).remote_humans is False


def test_remote_humans_needs_the_decisions_capability():
    from orch.addons.manifest import manifest_problems
    assert any("decisions" in p for p in manifest_problems({**GOOD, "remote_humans": True}))


def test_qr_payload_is_capped_in_utf8_bytes(ws):
    m = parse_manifest(GOOD)
    problems = widget_problems(QR("€" * 1000), slot="page.x", manifest=m)
    assert problems and any("bytes" in p for p in problems)
    assert widget_problems(QR("a" * 1000), slot="page.x", manifest=m) == []


def test_an_oversized_qr_renders_a_callout_never_a_500():
    from orch.dashboard.qr import qr_matrix
    from orch.dashboard.views import TEMPLATES
    assert qr_matrix("€" * 1000) is None
    macro = TEMPLATES.env.get_template("_widgets.html").module.widget
    html = str(macro(QR("€" * 1000, "cap"), None))
    assert 'role="img"' not in html and "callout" in html and "too long" in html  # no QR svg, only the icon
    assert '<svg viewBox' in str(macro(QR("short", "cap"), None))


def test_pairing_qr_bypasses_the_module_matrix_cache():
    """core's own QR (the pairing link, a one-time secret) never goes through the cached module-matrix
    path: its text must never sit in the lru_cache after the render."""
    from orch.dashboard import qr as qr_mod
    from orch.dashboard.views import qr_widget

    secret_text = "https://example.invalid/pair#super-secret-key-should-not-be-cached"
    w = qr_widget(secret_text, "Scan with the phone")
    assert w.uncached is True
    qr_mod._matrix.cache_clear()
    assert qr_mod.qr_matrix_uncached(secret_text) is not None
    assert qr_mod._matrix.cache_info().currsize == 0  # the uncached path never populated the cache


def test_qr_is_one_run_length_path():
    import re
    from orch.dashboard.qr import qr_matrix, qr_runs
    from orch.dashboard.views import TEMPLATES
    text = "https://example.invalid/pair#" + "x" * 300
    m = qr_matrix(text)
    runs = qr_runs(m)
    assert sum(w for _, _, w in runs) == sum(v for row in m for v in row)       # every dark module, once
    for x, y, w in runs:
        assert all(m[y][x:x + w]) and (x == 0 or not m[y][x - 1]) and (x + w == len(m) or not m[y][x + w])
    html = str(TEMPLATES.env.get_template("_widgets.html").module.widget(QR(text, "cap"), None))
    svg = html[html.index("<svg"):html.index("</svg>")]
    assert svg.count("<path") == 1 and svg.count("<rect") == 1                   # the white background only
    d = re.search(r'<path d="([^"]*)"', svg).group(1)
    assert re.fullmatch(r"(?:M\d+ \d+h\d+v1h-\d+z)+", d) and d.count("M") == len(runs)


def _src(la, name="phone.jpg", data=b"jpg"):
    src = la.ctx.state_dir / "in" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    return src


def test_add_artifact_refuses_a_file_over_the_cap(ws, aops, monkeypatch):
    from orch.addons import api
    t = aops.new("Artifacts")
    la = loaded(ws, object(), name="tixlike")
    monkeypatch.setattr(api, "MAX_ARTIFACT_BYTES", 10)
    with pytest.raises(ValidationError, match="larger"):
        la.ctx.ops().add_artifact(t.id, _src(la, data=b"x" * 11))
    assert not (ws.artifacts_dir / t.id).exists() or not any((ws.artifacts_dir / t.id).iterdir())
    assert la.ctx.ops().add_artifact(t.id, _src(la, "ok.jpg", b"x" * 10)).read_bytes() == b"x" * 10


def test_add_artifact_refuses_a_hard_link(ws, aops, tmp_path):
    t = aops.new("Artifacts")
    la = loaded(ws, object(), name="tixlike")
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    link = la.ctx.state_dir / "in" / "innocent.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.link(secret, link)
    with pytest.raises(ValidationError, match="link"):
        la.ctx.ops().add_artifact(t.id, link)


def test_add_artifact_copies_what_it_checked(ws, aops, monkeypatch):
    """A swap of the path after the check (and the open) never changes what is copied."""
    from orch.addons import api
    t = aops.new("Artifacts")
    la = loaded(ws, object(), name="tixlike")
    src = _src(la, data=b"checked")
    evil = la.ctx.state_dir / "evil"
    evil.write_bytes(b"swapped")
    real_open = os.open

    def swapping_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        if str(path) == str(src):
            assert flags & os.O_NOFOLLOW
            os.replace(evil, src)
        return fd
    monkeypatch.setattr(api.os, "open", swapping_open)
    assert la.ctx.ops().add_artifact(t.id, src).read_bytes() == b"checked"
