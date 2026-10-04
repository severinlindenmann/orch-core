"""Design tokens (design-system spec §2): static/tokens.json (DTCG 2025.10) is the source; static/tokens.css is
generated from it, committed, and must never drift."""
import json
import re
from pathlib import Path

from orch.dashboard.design import build

STATIC = Path(__file__).parents[1] / "src/orch/dashboard/static"


def test_tokens_css_is_generated_from_tokens_json():
    """Edit tokens.json, then run `python -m orch.dashboard.design.build`; never edit tokens.css by hand."""
    assert (STATIC / "tokens.css").read_text(encoding="utf-8") == build.render(build.load())


def test_check_mode_reports_drift(tmp_path):
    out = tmp_path / "tokens.css"
    out.write_text("/* stale */", encoding="utf-8")
    assert build.main(["--check", "--out", str(out)]) == 1
    assert build.main(["--out", str(out)]) == 0
    assert build.main(["--check", "--out", str(out)]) == 0


def test_every_alias_resolves():
    tokens = build.flatten(build.load())
    for path, (node, _type) in tokens.items():
        build.resolve(node["$value"], tokens)  # raises KeyError on a dangling {alias}
        for mode in ("dark", "compact", "coarse", "reduced"):
            ext = node.get("$extensions", {}).get(build.NS, {})
            if mode in ext:
                build.resolve(ext[mode], tokens)


def test_tokens_css_is_self_contained():
    """TIX copies tokens.css as is: it holds custom properties only, under theme, density and media selectors,
    never an app selector."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "tokens.css").read_text(encoding="utf-8"), flags=re.S)
    allowed = re.compile(r"^(:root|\[data-theme=(light|dark|system)\]|:root:not\(\[data-theme=light\]\)"
                         r"|\[data-density=compact\])$")
    for selector, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        for s in selector.split(","):
            assert allowed.match(s.strip()), s
        for decl in filter(None, (d.strip() for d in body.split(";"))):
            assert decl.startswith("--") or decl.startswith("color-scheme:"), decl
    assert "url(" not in css


def test_existing_short_names_and_new_semantic_names_are_defined():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    for name in ("bg", "surface", "surface2", "line", "line2", "text", "muted", "chip", "hover", "mint", "focus",
                 "ctl", "ctl-overlay", "link", "raised", "overlay", "primary", "on-primary", "danger", "on-danger",
                 "nav-active", "scrim", "type-title", "type-section", "type-item", "type-body", "type-control",
                 "type-meta", "s-1", "s-4", "s-12", "r-sm", "r-md", "r-lg", "r-pill", "shadow-raised",
                 "shadow-overlay", "dur-1", "dur-2", "dur-3", "ease-out", "ease-in", "control-h", "row-h", "card-pad"):
        assert re.search(rf"--{name}:", css), name


def test_modes_density_pointer_phone_and_reduced_motion():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    compact = re.search(r"\[data-density=compact\] \{([^}]*)\}", css).group(1)
    assert "--row-h: 32px" in compact and "--control-h: 32px" in compact and "--card-pad: 12px" in compact
    coarse = re.search(r"@media \(pointer: coarse\) \{\s*:root \{([^}]*)\}", css).group(1)
    assert "--control-h: 44px" in coarse and "--row-h: 44px" in coarse
    phone = re.search(r"@media \(max-width: 899px\) \{\s*:root \{([^}]*)\}", css).group(1)
    assert re.search(r"--type-title: 800 24px/", phone) and re.search(r"--type-control: 600 16px/", phone)
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{\s*:root \{([^}]*)\}", css).group(1)
    for name in ("dur-1", "dur-2", "dur-3"):
        assert f"--{name}: 0ms" in reduced
    base = re.search(r"\n:root \{([^}]*)\}", css).group(1)
    assert "--dur-1: 100ms" in base and "--dur-2: 200ms" in base and "--dur-3: 280ms" in base
    for size in ("28px", "18px", "15px", "14px", "12px"):  # the five-step type scale
        assert size in base


def test_aliases_to_named_tokens_stay_references():
    """`--control-border` follows `--ctl` in every theme instead of freezing the light value."""
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    assert "--control-border: var(--ctl);" in css


def test_item_type_uses_the_variable_weight():
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    assert re.search(r"--type-item: 650 15px/", css)


def test_breakpoints_come_from_the_tokens():
    assert build.breakpoints() == {"shell": 900, "split": 1100, "wide": 1440, "narrow": 400, "medium": 560, "wide_container": 800}


def test_layout_links_tokens_before_app_css(dash):
    head = dash.get("/").text.split("</head>", 1)[0]
    assert head.index("/static/tokens.css") < head.index("/static/app.css")
    assert dash.get("/static/tokens.css").status_code == 200


def test_app_css_has_no_theme_token_blocks():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for name in ("bg", "surface", "text", "muted", "ok-bg", "you-mark", "mint", "focus"):
        assert not re.search(rf"--{name}\s*:", css), name


def test_tokens_json_is_dtcg():
    doc = json.loads((STATIC / "tokens.json").read_text(encoding="utf-8"))
    assert doc["$extensions"][build.NS]["version"]
    flat = build.flatten(doc)
    assert all("$value" in node for node, _ in flat.values())
    assert all(t for _, t in flat.values()), "every token has a $type (directly or from its group)"


def test_locked_page_also_gets_tokens(dash):
    """The 401 "Locked" page is drawn without layout.html; it needs the tokens as well."""
    from fastapi.testclient import TestClient
    locked = TestClient(dash.app).get("/").text
    assert locked.index("/static/tokens.css") < locked.index("/static/app.css")


def test_phone_inputs_are_16px_so_ios_does_not_zoom():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    block = re.search(r"@media \(max-width: 899px\) \{(.*?)\n\}", css, re.S).group(1)
    assert re.search(r"input:not\(\[type=checkbox\]\):not\(\[type=radio\]\), select, textarea \{ font-size: 16px; \}", block)


def _keys(block: str) -> set[str]:
    return set(re.findall(r"(--[\w-]+):", block))


def test_a_light_wrapper_only_resets_themed_tokens():
    """A [data-theme=light] wrapper (the /design gallery inside a dark page) switches colours and shadows only; it
    must not reset density, coarse-pointer, phone type or reduced-motion values set further up."""
    css = (STATIC / "tokens.css").read_text(encoding="utf-8")
    base = re.search(r"\n:root \{([^}]*)\}", css).group(1)
    light = re.search(r"\n\[data-theme=light\] \{([^}]*)\}", css).group(1)
    dark = re.search(r"\n\[data-theme=dark\] \{([^}]*)\}", css).group(1)
    assert _keys(light) == _keys(dark)
    assert "--row-h" not in _keys(light) and "--dur-1" not in _keys(light) and "--type-title" not in _keys(light)
    assert {"--row-h", "--dur-1", "--type-title", "--bg"} <= _keys(base)
    assert not re.search(r":root, \[data-theme=light\]", css)
