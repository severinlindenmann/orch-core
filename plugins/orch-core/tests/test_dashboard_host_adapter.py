"""The dashboard must not depend on its own address, cookie or storage (docs/dashboard-frame.md): the browser
features that need them are used only inside the host adapter at the top of static/app.js, and the routes that carry
their own Content-Security-Policy are listed. The adapter itself is tested by tests/js/host_adapter.js."""
import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "orch" / "dashboard" / "static"
DOC = ROOT / "docs" / "dashboard-frame.md"

# Own scripts only: vendor/ is third-party, widgets/ run in their own frames with their own policy.
BROWSER_FEATURES = re.compile(r"\b(?:location|history|localStorage|sessionStorage)\b|document\.cookie|navigator\.clipboard")


def _outside_adapter(text: str) -> str:
    return re.sub(r"// host-adapter:begin.*?// host-adapter:end", "", text, flags=re.S)


_TOKEN = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`", re.S)


def _template(text: str, i: int):
    """From just after an opening backtick: (the code inside its ${...} parts, index after the closing backtick)."""
    code = []
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return " ".join(code), i + 1
        elif text.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            code.append(_code(text[i + 2:j - 1]))
            i = j
        else:
            i += 1
    return " ".join(code), i


def _code(text: str) -> str:
    """The script in one pass, without comments and string contents (a word in a message or a note is not a use).
    The parts of a template literal inside ${...} are code and stay."""
    out, i = [], 0
    while True:
        m = _TOKEN.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        if m.group() == "`":
            inner, i = _template(text, m.end())
            out.append('"" ' + inner + " ")
        else:
            out.append("\n" * m.group().count("\n") if m.group().startswith(("/*", "//")) else '""')
            i = m.end()


@pytest.mark.parametrize("snippet", [
    "const u = location.href;",
    'const s = "a // b" + location.href;',
    "const t = `x ${window.history.length} y`;",
    "const u = `${`${document.cookie}`}`;",
    "a = '\\'' + sessionStorage.getItem('k');",
    "/* note */ navigator.clipboard.writeText(x);",
])
def test_scanner_catches_a_violation(snippet):
    assert BROWSER_FEATURES.search(_code(snippet)), snippet


@pytest.mark.parametrize("snippet", [
    "// location.href is only a note",
    "/* history.pushState */ x = 1;",
    'const s = "location.href";',
    "const s = 'localStorage';",
    "const t = `the location is ${place}`;",
    'const u = "http://x/y"; // document.cookie',
])
def test_scanner_ignores_comments_and_strings(snippet):
    assert not BROWSER_FEATURES.search(_code(snippet)), snippet


def test_browser_features_are_only_used_inside_the_adapter():
    scripts = sorted(STATIC.glob("*.js"))
    assert {p.name for p in scripts} >= {"app.js", "terminal.js"}
    bad = []
    for p in scripts:
        text = p.read_text(encoding="utf-8")
        if p.name == "app.js":
            assert "// host-adapter:begin" in text and "// host-adapter:end" in text
            text = _outside_adapter(text)
        for n, line in enumerate(_code(text).splitlines(), 1):
            if BROWSER_FEATURES.search(line):
                bad.append(f"{p.name}: {line.strip()[:100]}")
    assert not bad, "use window.orchHost (static/app.js, host adapter) instead:\n" + "\n".join(bad)


def test_adapter_is_the_first_thing_app_js_defines():
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    assert text.index("// host-adapter:begin") < text.index("document.addEventListener")


def test_pages_render_their_own_path(dash, put):
    tid = put("open", title="Something to show")
    assert re.search(r'<html [^>]*data-path="/"', dash.get("/").text)
    r = dash.get(f"/t/{tid}?open=all")
    assert f'data-path="/t/{tid}?open=all"' in r.text


def _dashboard_routes():
    """Every GET route of every dashboard router module (found by name, so a new module is covered)."""
    import importlib
    import pkgutil

    import orch.dashboard as pkg
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("routes_"):
            router = getattr(importlib.import_module(f"orch.dashboard.{info.name}"), "router", None)
            for route in getattr(router, "routes", []):
                if "GET" in getattr(route, "methods", ()):
                    yield route


def test_routes_with_their_own_csp_are_listed():
    listed = set(re.findall(r"^\| `(/[^`]+)` \|", DOC.read_text(encoding="utf-8"), flags=re.M))
    actual, every = set(), set()
    for route in _dashboard_routes():
        every.add(route.path)
        if re.search(r"HEADERS|SANDBOX_CSP|Content-Security-Policy", inspect.getsource(route.endpoint)):
            actual.add(route.path)
    assert actual, "the route scan found nothing; the test would pass for the wrong reason"
    assert actual <= listed, f"routes with their own Content-Security-Policy missing from {DOC.name}: {sorted(actual - listed)}"
    assert listed <= every, f"{DOC.name} lists routes that do not exist: {sorted(listed - every)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_host_adapter_local_default_and_fallbacks():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "host_adapter.js"), str(STATIC / "app.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "host adapter ok" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_swap_and_live_refresh_ordering():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "page_swap.js"), str(STATIC / "app.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "page swap ok" in r.stdout
