"""F: no new raw px values in app.css. Spacing, sizes, radii and type come from the tokens (tokens.css: --s-*, --r-*,
--control-h, --type-*, ...). Exempt: hairlines and outline offsets (<= 2px), layout widths (>= 80px: column and
container sizes), and the numbers inside @media / @container conditions (CSS cannot read custom properties there).
ALLOWED is a ceiling for the raw values the file had when the lint arrived: it may only go down. A new raw value
fails here; use a token, or, for a real exception, raise its count below with a reason in the commit."""
import collections
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css"

ALLOWED = {"3px": 8, "4px": 14, "5px": 2, "6px": 32, "7px": 1, "8px": 25, "9px": 1, "10px": 30, "11px": 4, "12px": 51,
           "13px": 4, "14px": 18, "15px": 1, "16px": 27, "17px": 1, "18px": 12, "20px": 13, "22px": 11, "24px": 8,
           "28px": 1, "32px": 7, "40px": 3, "44px": 7, "56px": 1, "60px": 1, "64px": 1}


def raw_px(css: str) -> collections.Counter:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@(?:media|container)[^{]*\{", "{", css)  # conditions may name breakpoints in px
    found = (m.group(0) for m in re.finditer(r"(?<![\w.-])\d+(?:\.\d+)?px\b", css))
    return collections.Counter(v for v in found if 2 < float(v[:-2]) < 80)


def test_no_new_raw_px_values_in_app_css():
    over = {v: (n, ALLOWED.get(v, 0)) for v, n in raw_px(CSS.read_text(encoding="utf-8")).items() if n > ALLOWED.get(v, 0)}
    assert not over, f"raw px values beyond the allow-list (found, allowed): {over}; use a token from tokens.css"


def test_the_lint_sees_a_new_value_and_ignores_the_exempt_ones():
    assert raw_px(".x { padding: 7px 13px; }") == {"7px": 1, "13px": 1}
    assert raw_px(".x { border: 1px solid; outline-offset: 2px; grid-template-columns: 240px; }") == {}
    assert raw_px("@media (max-width: 720px) { .x { margin: var(--s-2); } } /* 12px */") == {}
    assert raw_px(".x { width: calc(var(--s-8) + var(--s-1)); }") == {}
