import json

import pytest

from addon_fixtures import GOOD, make_addon
from orch.addons import discovery, manage, userfiles
from orch.addons.discovery import custom_addons_dir
from orch.core.events import Actor

DASH = Actor("human", "you", "dashboard")  # manage refuses without a human

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def addons(tmp_path, monkeypatch):
    """A default `alpha` and an installed, untrusted custom `hello-status`."""
    defaults = tmp_path / "defaults"
    alpha = make_addon(defaults, manifest={**GOOD, "name": "alpha", "title": "Alpha <b>x</b>"})
    alpha.rename(defaults / "alpha")
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: defaults)
    src = make_addon(tmp_path / "src")
    manage.install(str(src), actor=DASH)
    return src


@pytest.fixture
def client(ws, addons):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _table(html):
    return html[html.index('id="addons-h"'):]


def test_table_lists_both_kinds(client):
    t = _table(client.get("/workspace").text)
    assert "Alpha &lt;b&gt;x&lt;/b&gt;" in t and "orch-core plugin" in t and "default" in t
    assert "hello-status" in t and "custom" in t and "not trusted yet" in t and "0.1.0" in t
    assert 'action="/workspace/addons/hello-status/trust"' in t and "new binary: git" in t
    enable_hello = t[t.index('action="/workspace/addons/hello-status/enable"'):][:400]
    assert "disabled" in enable_hello


def test_enable_a_default_addon_loads_it(client, ws):
    r = client.post("/workspace/addons/alpha/enable", data={"enabled": "1"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Enabled+alpha" in r.headers["location"]
    assert userfiles.workspace_addons(ws.root)["alpha"]["enabled"] is True
    html = client.get("/").text
    assert 'href="/addons/alpha/"' in html  # reloaded: the menu has its page now
    client.post("/workspace/addons/alpha/enable", data={"enabled": "0"}, headers=ORIGIN)
    assert 'href="/addons/alpha/"' not in client.get("/").text


def test_posts_need_origin(client, ws):
    for url, data in (("/workspace/addons/alpha/enable", {"enabled": "1"}), ("/workspace/addons/hello-status/trust", {"seen": "x"}),
                      ("/workspace/addons/alpha/settings", {"greeting": "x"}), ("/workspace/addons/check-updates", {})):
        assert client.post(url, data=data, follow_redirects=False).status_code == 403, url
    assert userfiles.workspace_addons(ws.root) == {}


def test_enable_untrusted_is_refused(client, ws):
    r = client.post("/workspace/addons/hello-status/enable", data={"enabled": "1"}, headers=ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"] and "trust+it+first" in r.headers["location"]
    assert userfiles.workspace_addons(ws.root) == {}


def test_trust_needs_the_digest_that_was_shown(client):
    stale = client.post("/workspace/addons/hello-status/trust", data={"seen": "0" * 64}, headers=ORIGIN, follow_redirects=False)
    assert "changed+since+you+reviewed" in stale.headers["location"]
    digest = manage.review("hello-status").digest
    ok = client.post("/workspace/addons/hello-status/trust", data={"seen": digest}, headers=ORIGIN, follow_redirects=False)
    assert "Trusted+hello-status" in ok.headers["location"]
    # ruling F7: hello-status's own state, not the always-trusted default addon's
    assert userfiles.trust_state(discovery.find("hello-status")) == "trusted"
    assert "not trusted yet" not in _table(client.get("/workspace").text)


def test_changed_addon_shows_retrust_and_cannot_be_enabled(client, ws):
    manage.trust_addon("hello-status", actor=DASH)
    manage.enable(ws.root, "hello-status", actor=DASH)
    (custom_addons_dir() / "hello-status" / "README.md").write_text("changed\n", encoding="utf-8")
    client.app.state.addons.reload()
    t = _table(client.get("/workspace").text)
    assert "changed, re-trust" in t and "~ README.md" in t and "Re-trust" in t
    assert "changed since you trusted it" in t  # the load problem of the enabled addon


def test_settings_save_and_validate(client, ws):
    bad = client.post("/workspace/addons/alpha/settings", data={"greeting": "a\nb"}, headers=ORIGIN, follow_redirects=False)
    assert "err=" in bad.headers["location"]
    ok = client.post("/workspace/addons/alpha/settings", data={"greeting": "Hi", "x": "y"}, headers=ORIGIN, follow_redirects=False)
    assert "Saved+settings" in ok.headers["location"]
    assert userfiles.workspace_addons(ws.root)["alpha"]["config"] == {"greeting": "Hi"}
    assert 'value="Hi"' in client.get("/workspace").text


def test_check_for_updates(client, addons):
    data = json.loads((addons / "orch-addon.json").read_text())
    (addons / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0"}))
    r = client.post("/workspace/addons/check-updates", headers=ORIGIN, follow_redirects=False)
    assert "1+update" in r.headers["location"]
    t = _table(client.get("/workspace").text)
    assert "update available 0.1.0 → 0.2.0" in t and 'data-copy="orch addon update hello-status"' in t


def test_suggested_addons(ws, configure, addons):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    ws = configure(suggested_addons=["alpha", "hello-status", "nope", "Bad/Name"])
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    html = c.get("/workspace").text
    s = html[html.index('id="suggested-h"'):]
    s = s[:s.index("</section>")]
    assert "Enable?" in s and "trust it first" in s and "not installed" in s and "Bad/Name" not in s


def test_workspace_badge_counts_addons_needing_attention(client, ws):
    manage.trust_addon("hello-status", actor=DASH)
    manage.enable(ws.root, "hello-status", actor=DASH)
    (custom_addons_dir() / "hello-status" / "README.md").write_text("changed\n", encoding="utf-8")
    client.app.state.addons.reload()
    assert client.app.state.addons.attention() == 1


@pytest.mark.parametrize("text", ["{", "[]", '{"x": {"addons": []}}'])
def test_workspace_page_survives_corrupt_user_files(client, text):
    from orch.dashboard.launch import config_dir
    for name in ("workspaces.json", "addons.json"):
        (config_dir() / name).write_text(text, encoding="utf-8")
    client.app.state.addons.reload()
    assert client.get("/workspace").status_code == 200
    assert client.get("/").status_code == 200


def test_enable_loads_in_the_post_not_in_the_next_page(client, monkeypatch):
    """Ruling F5: the enable POST re-loads the registry; the next GET imports nothing and starts no process."""
    import subprocess
    from orch.addons.loader import AddonRegistry
    client.post("/workspace/addons/alpha/enable", data={"enabled": "1"}, headers=ORIGIN)

    def refuse(*a, **k):
        raise AssertionError("no addon load or subprocess during a page render")
    monkeypatch.setattr(AddonRegistry, "load", classmethod(refuse))
    monkeypatch.setattr(subprocess, "Popen", refuse)
    from orch.dashboard import views
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    html = client.get("/").text
    assert 'href="/addons/alpha/"' in html


def test_settings_show_for_a_trusted_addon_that_is_not_enabled(client, ws):
    t = _table(client.get("/workspace").text)
    assert 'action="/workspace/addons/alpha/settings"' in t and 'value="Hello"' in t
    assert 'action="/workspace/addons/hello-status/settings"' not in t  # not trusted: no settings yet


def test_trust_review_warns_when_phones_may_act(ws, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    manage.install(str(make_addon(tmp_path / "src", manifest={**GOOD, "remote_humans": True,
                                                              "capabilities": [*GOOD["capabilities"], "decisions"]})),
                   actor=DASH)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    t = _table(c.get("/workspace").text)
    review = t[t.index('id="trust-hello-status"'):]
    assert '<span class="chip chip-warn"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg> new permission: paired phones may answer ' \
           'and decide for you (remote_humans)</span>' in review
