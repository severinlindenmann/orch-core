"""Colour rules (design-system spec §2.2). Values are read from static/tokens.json, the source of tokens.css,
never parsed out of app.css."""
import re
from pathlib import Path

import pytest

from orch.dashboard.design import build

pytest.importorskip("fastapi")

STATIC = Path(__file__).parents[1] / "src/orch/dashboard/static"
CSS = STATIC / "app.css"
TPL = Path(__file__).parents[1] / "src/orch/dashboard/templates"
ROLES = ("ok", "info", "you", "warn", "err", "neu")


def _themes() -> dict[str, dict[str, str]]:
    """{"light": {"--bg": "#F2F3F5", ...}, "dark": {...}} for every colour token, aliases resolved."""
    doc = build.load()
    tokens = build.flatten(doc)
    out = {"light": {}, "dark": {}}
    for node, typ in tokens.values():
        ext = node.get("$extensions", {}).get(build.NS, {})
        if typ != "color" or "css" not in ext:
            continue
        light = build.resolve(node["$value"], tokens)
        dark = build.resolve(ext.get("dark", node["$value"]), tokens)
        if "alpha" in light:
            continue  # the scrim is not a text or mark colour
        out["light"][ext["css"]] = light["hex"]
        out["dark"][ext["css"]] = dark["hex"]
    return out


def _lum(hex_: str) -> float:
    c = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


THEMES = _themes()


def _rules(css: str):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list contains exactly `selector`."""
    for sel, body in _rules(css):
        if selector == " ".join(sel.split()) or selector in [s.strip() for s in sel.split(",")]:
            return body
    raise AssertionError(f"no rule for {selector}")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_role_tokens_in_every_theme(theme):
    for role in ROLES:
        for part in ("bg", "fg", "mark"):
            assert f"--{role}-{part}" in THEMES[theme], (role, part)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_pairs_reach_4_5(theme):
    t = THEMES[theme]
    pairs = [("--text", "--bg"), ("--text", "--surface"), ("--muted", "--surface"), ("--muted", "--bg"),
             ("--muted", "--surface2"), ("--muted", "--chip"), ("--link", "--surface"), ("--link", "--bg"),
             ("--on-primary", "--primary"), ("--on-danger", "--danger"), ("--text", "--nav-active")]
    pairs += [(f"--{r}-fg", f"--{r}-bg") for r in ROLES] + [(f"--{r}-fg", "--surface") for r in ROLES]
    for fg, bg in pairs:
        assert contrast(t[fg], t[bg]) >= 4.5, (theme, fg, bg, round(contrast(t[fg], t[bg]), 2))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_control_borders_marks_and_focus_reach_3(theme):
    """WCAG 1.4.11: control boundaries, focus rings and meaningful marks are >= 3:1 where they sit."""
    t = THEMES[theme]
    pairs = [("--ctl", "--bg"), ("--ctl", "--surface"), ("--ctl", "--surface2"),
             ("--ctl-overlay", "--overlay"), ("--ctl-overlay", "--chip"), ("--ctl-overlay", "--nav-active"),
             ("--focus", "--bg"), ("--focus", "--surface"),
             ("--you-mark", "--bg")]
    pairs += [(f"--{r}-mark", "--surface") for r in ROLES]
    for fg, bg in pairs:
        assert contrast(t[fg], t[bg]) >= 3, (theme, fg, bg, round(contrast(t[fg], t[bg]), 2))


def test_spec_fixes_are_in_the_tokens():
    light, dark = THEMES["light"], THEMES["dark"]
    assert (light["--link"], dark["--link"]) == ("#0A6B5A", "#5BE0B8")
    assert (light["--ctl"], dark["--ctl"]) == ("#7A838D", "#6B7480")
    assert (light["--you-mark"], dark["--you-mark"]) == ("#E0287F", "#F84A9B")
    assert (light["--danger"], dark["--danger"]) == ("#B42318", "#FF8A80")
    assert (light["--primary"], dark["--primary"]) == ("#15171A", "#E6E8EB")  # R1/D3: the primary is ink
    assert light["--nav-active"] != light["--ok-bg"] and dark["--nav-active"] != dark["--ok-bg"]


def test_old_chip_classes_are_gone():
    for path in TPL.glob("*.html"):
        assert not re.search(r"chip-(soft|bad)\b", path.read_text()), path


def test_status_macro_has_icon_and_text(dash, put):
    put("testing", sections={"Verification": "ok"})
    html = dash.get("/").text
    assert re.search(r'class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> ', html)


def test_mint_is_never_a_fill_text_or_button():
    """R1/D3 (this replaces the v2 rule "mint only fills .btn-primary"): mint is the brand, the nav-active bar and
    the dark focus ring; never a background fill, a text colour or a button. Primary buttons are ink."""
    css = CSS.read_text()
    for selector, body in _rules(css):
        assert not re.search(r"background(?:-color)?\s*:\s*[^;]*var\(--mint\)", body), selector
        assert not re.search(r"(?<![-\w])color\s*:\s*var\(--mint\)", body), selector
    body = _rule(css, ".btn-primary")
    assert "var(--primary)" in body and "var(--on-primary)" in body


def test_nav_active_is_neutral_with_a_mint_bar():
    body = _rule(CSS.read_text(), ".menu a.item[aria-current=page]")
    assert "var(--nav-active)" in body and "var(--mint)" in body and "ok-bg" not in body


def test_links_use_the_link_colour_and_running_text_links_are_underlined():
    css = CSS.read_text()
    assert "var(--link)" in _rule(css, ".lnk")
    running = _rule(css, "main.content :is(p, dd, .callout, .cell-text, .section li) a:not(.btn)")
    assert "text-decoration: underline" in running


def test_controls_use_the_control_border():
    css = CSS.read_text()
    for selector in (".btn", "input:not([type=checkbox]):not([type=radio]):not([type=file])", ".filter-chip"):
        assert "var(--ctl)" in _rule(css, selector), selector
    assert "--ctl: var(--ctl-overlay)" in _rule(css, "dialog")


def test_disabled_primary_is_drawn_neutral():
    body = _rule(CSS.read_text(), ".btn-primary:disabled")
    assert "var(--chip)" in body and "var(--muted)" in body


def test_selected_tab_count_keeps_full_contrast():
    css = CSS.read_text()
    for selector, body in _rules(css):
        if ".count" in selector and ".tabs" in selector:
            assert "opacity" not in body, selector


def test_legacy_colour_tokens_are_gone():
    """Every status colour comes from the semantic role tokens (spec §5); the pre-v2 tokens
    (--soft, --onsoft, --warn, --warnbg, --bad, --badbg) and the v2 stand-ins the design system replaced
    (--accent, --active, --onmint) are neither defined nor used."""
    css = CSS.read_text() + (STATIC / "tasks.css").read_text()
    for name in ("soft", "onsoft", "warn", "warnbg", "bad", "badbg", "accent", "active", "onmint"):
        assert not re.search(rf"--{name}\s*[:)]", css), name


def test_waiting_status_is_the_you_role():
    """Visual review Board #3 / spec D6: a ticket waiting on the human is `you` everywhere, not `warn`."""
    from orch.dashboard.data.metrics import STATUS_LABELS, STATUS_ROLES
    assert STATUS_ROLES["waiting"] == "you"
    assert STATUS_LABELS["waiting"] == "Waiting"
