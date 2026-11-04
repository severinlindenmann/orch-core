"""Regenerate with: cd packages/publish && uv run python ../../docs/mockups/publish/render.py.

No server or database is opened. Frames are inert snapshots: scripts, forms and
navigation are sandboxed; the landing form's visibility is frozen as after its script.
"""

import re
import sys
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

PUBLISH = Path(__file__).resolve().parents[3] / "packages" / "publish"
sys.path.insert(0, str(PUBLISH))
sys.path.insert(0, str(PUBLISH / "host"))

from orch_apps_host import server as H

from pubsrv import appserve as A
from pubsrv import serve as S

SHARE = "1" * 32
VIEW = "2" * 32


def landing(password=False, missing=False):
    document = S.landing_page(SHARE, password).body.decode()
    # srcdoc has no fragment and scripts must stay disabled. Freeze only visibility;
    # all markup, styles and copy still come from the real landing renderer.
    if missing:
        return document.replace('<p id="x" hidden>', '<p id="x">')
    return document.replace(f'action="/s/{SHARE}/open" hidden>', f'action="/s/{SHARE}/open">')


class HostPreview(H.Handler):
    """Capture the host's real refusal renderer without opening a socket."""

    def __init__(self):
        self.command = "GET"

    def _app_with_hash(self, slug):
        return None

    def _cookie(self, name):
        return None

    def _send(self, status, body=b"", **kwargs):
        self.document = body.decode()


def states():
    app = A.AppServer(None, None, lambda: 0, "https://apps.invalid", None, lambda scope: True)
    panels = [
        ("Password prompt", landing(password=True)),
        ("Wrong password", S.page(401, S.WRONG_PW).body.decode()),
        ("Open shared files", landing()),
        ("Needs its secret link — missing # part", landing(missing=True)),
    ]
    with TemporaryDirectory() as directory:
        for name in ("Project notes.txt", "report.pdf"):
            Path(directory, name).touch()
        response = S.ShareServer._listing(None, directory, SHARE, VIEW)
        panels.insert(2, ("Shared files — two files", response.body.decode()))
    for title, response in [
        ("App loader — Opening", app._opening_page("demo", VIEW)),
        ("Not found", S.not_found()),
        *(
            (("Already opened" if name == "opened" else name.capitalize()), S.gone(name))
            for name in ("opened", "expired", "removed")
        ),
        ("View ended", S.gone("view")),
        ("Needs sign-in", app.page(403, A.NO_SIGN_IN)),
        ("Not allowed", S.page(403, S.NOT_ALLOWED)),
        ("Not allowed — use Open", S.page(405, S.USE_OPEN)),
        ("Too large", S.page(413, S.TOO_LARGE)),
        ("Slow down", S.page(429, S.TOO_MANY)),
        ("Busy", S.page(503, S.BUSY)),
        ("Unavailable", S.page(503, S.UNAVAILABLE)),
        ("Error", S.page(500, S.ERROR)),
        ("App password prompt", app._gate_page("demo")),
        ("App wrong password", app._gate_page("demo", A.WRONG, 401)),
        ("App not found", app.page(404, A.NOT_FOUND)),
        ("Stopped", app.page(410, A.STOPPED)),
        ("App view ended", app.page(410, A.VIEW_ENDED)),
    ]:
        panels.append((title, response.body.decode()))
    host = HostPreview()
    host._auth_app("demo")
    panels.append(("Host app needs its secret link", host.document))
    host._not_found()
    panels.append(("Host not found", host.document))
    loader = H.LOADER.replace("__SHARE_ID__", SHARE)
    panels.append(("Sealed app loader", loader))
    # The loader's failure messages live in JS. Freeze each real say() message in
    # its existing message element; no async fetch/decryption runs in the gallery.
    for message in re.findall(r'say\("([^"\n]+)"\)', loader):
        document = re.sub(
            r'(<div class="msg" id="msg">).*?(</div>)',
            lambda match, message=message: match[1] + escape(message) + match[2],
            loader,
        )
        panels.append(("Sealed share — " + message, document))
    return panels


def render():
    panels = "\n".join(
        f'<section><h2>{escape(title)}</h2><iframe inert sandbox title="{escape(title, quote=True)}" '
        f'srcdoc="{escape(document, quote=True)}"></iframe></section>'
        for title, document in states()
    )
    return (
        """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Publish visitor pages</title>
<style>
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui,sans-serif; margin: 0 auto; padding: 24px; max-width: 1500px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,440px),1fr)); gap: 24px; }
h1 { margin-bottom: 8px; } h2 { font-size: 16px; margin: 0; padding: 12px; }
section { border: 1px solid #888; border-radius: 8px; overflow: hidden; }
iframe { width: 100%; height: 440px; display: block; border: 0; border-top: 1px solid #888; }
</style>
<h1>Publish visitor pages</h1>
<p>Generated from the product renderers. Each frame is an inert visitor state;
forms, navigation and scripts are disabled. Colors follow your system preference.</p>
<div class="gallery">
"""
        + panels
        + "\n</div>\n</html>\n"
    )


if __name__ == "__main__":
    Path(__file__).with_name("index.html").write_text(render(), encoding="utf-8")
