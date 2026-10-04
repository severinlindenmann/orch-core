"""R24: every href and src in ticket Markdown is canonicalised once (decoded, dot segments resolved, scheme and host
split off) before the renderer decides. Live only: a bound artifact of this ticket, another ticket's artifact as the
canonical /a/<ID>/<name>, an http(s)/mailto URL or a clean root-relative path not under /a/, or an in-page #fragment."""
import hashlib
import re
from urllib.parse import urljoin

import pytest

pytest.importorskip("markdown_it")

from orch.core.artifacts import inline_names  # noqa: E402
from orch.dashboard.markdown import ArtifactScope, render_markdown  # noqa: E402

T = "T-1"
SHA = hashlib.sha256(b"x").hexdigest()
SCOPE = ArtifactScope(T, {"login.png": {"name": "login.png", "sha256": SHA}}, None)
BOUND = f"http://h/a/{T}/login.png?v={SHA[:16]}"

PROBES = [
    "[a](/a/{t}/login.png)", "[a](/a/./{t}/login.png)", "[a](/a/X/../{t}/login.png)", "[a](/x/../a/{t}/login.png)",
    "[a](/./a/{t}/login.png)", "[a](../a/{t}/login.png)", "[a](a/{t}/login.png)", "[a](/a//{t}/login.png)",
    "[a](/A/{t}/login.png)", "[a](//127.0.0.1:8765/a/{t}/login.png)", "[a](http://127.0.0.1:8765/a/{t}/login.png)",
    "[a](/a/{t}/login.png/)", "[a](/a/{t}%2Flogin.png)", "[a](/a/{t}/login%252Epng)", "[a](\\a\\{t}\\login.png)",
    "[a](/a\\{t}\\login.png)", "[a](artifacts/../artifacts/{t}/login.png)", "[a](/a/%2E/{t}/login.png)",
    "[a](/a/X/%2E%2E/{t}/login.png)", "[a](/a/{t}/./login.png)", "[a][r]\n\n[r]: /a/./{t}/login.png",
    "[a][r]\n\n[r]: /a/X/%2E%2E/{t}/login.png", "<http://127.0.0.1:8765/a/{t}/login.png>",
    "![a](/a/./{t}/login.png)", "![a](/x/../a/{t}/login.png)", "![a](/a/X/../{t}/login.png)",
    "![a](//127.0.0.1:8765/a/{t}/login.png)", "[a](%2Fa/{t}/login.png)", "[a](/a/{t}/login.png?x#y)",
    "[a](  /a/{t}/login.png  )", "[a](/a/{t}/login.png%3Fv=1)", "[a](/a/{t}/login.png%23f)",
    "[a](/a/{t}/Login.png)", "[a](HTTPS://127.0.0.1/A/{t}/login.png)",
    "[a](///127.0.0.1:8765/a/{t}/login.png)", "[a](////127.0.0.1:8765/a/{t}/login.png)",
    "[a](/\\127.0.0.1:8765/a/{t}/login.png)", "[a](///localhost:8765/a/{t}/login.png)",
    "![a](///127.0.0.1:8765/a/{t}/login.png)", "[a](http:///127.0.0.1:8765/a/{t}/login.png)",
]


def _live_artifact_urls(html):
    hrefs = re.findall(r'(?:href|src)="([^"]*)"', html)
    resolved = [urljoin("http://h/t/T-1", h.replace("&amp;", "&")) for h in hrefs]
    return [r for r in resolved if "/a/" in r.lower() or "%2f" in r.lower()]


@pytest.mark.parametrize("tid", ["T-1", "t-1"])
@pytest.mark.parametrize("case", PROBES)
def test_no_live_link_to_this_tickets_artifacts_unless_bound(case, tid):
    text = case.format(t=tid)
    live = _live_artifact_urls(render_markdown(text, SCOPE))
    if "login.png" in inline_names(text, T):
        assert set(live) <= {BOUND}
    else:
        assert live == []


@pytest.mark.parametrize("text, href", [
    ("[a](https://ex.com/x)", "https://ex.com/x"), ("[a](/board)", "/board"), ("[a](#proven)", "#proven"),
    ("[a](mailto:a@ex.com)", "mailto:a@ex.com"), ("[a](/a/L-2/x.png)", "/a/L-2/x.png"),
    ("[a](../artifacts/L-2/x.png)", "/a/L-2/x.png"), ("[a](/a/L%2D2/x.png)", "/a/L-2/x.png"),
])
def test_allowed_links_stay_live(text, href):
    assert f'href="{href}"' in render_markdown(text, SCOPE)


@pytest.mark.parametrize("text", [
    "[a](https://ex.com/a/page)", "[a](board)", "[a](./board)", "[a](/x/../board)", "[a](/a/l-2/x.png)",
    "[a](/a/L-2/../x.png)", "[a](/a/L-2/sub/x.png)", "[a](/a/t-1/login.png)", "[a](ftp://ex.com/x)", "[a](?q=1)",
    "[a](http://127.0.0.1:8765/a/L-2/x.png)", "[a](///board)", "[a](///ex.com/board)", "[a](/\\ex.com/board)",
])
def test_everything_else_is_inert(text):
    html = render_markdown(text, SCOPE)
    assert "href=" not in html and "md-artifact-missing" in html


def test_images_follow_the_same_rule():
    html = render_markdown("![a](/a/L-2/x.png) ![b](https://ex.com/a.png) ![c](/a/t-1/login.png)", SCOPE)
    assert "<img" not in html
    assert 'href="/a/L-2/x.png"' in html and 'href="https://ex.com/a.png"' in html
    assert "/a/t-1" not in html
