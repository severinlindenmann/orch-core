import stat

import pytest

from addon_fixtures import loaded
from orch.addons.api import PairingTarget
from orch.addons.loader import AddonRegistry
from orch.remote import store

ORIGIN = {"origin": "http://testserver"}


def test_pair_stores_a_32_byte_key_owner_only(ws):
    phone, code = store.pair(ws.root, label="iPhone", addon="tixlike")
    assert len(phone.key) == 32 and phone.id.startswith("ph_") and len(code) == 6 and code.isdigit()
    assert code == store.check_code(phone.key)
    assert stat.S_IMODE(store.path().stat().st_mode) == 0o600
    assert store.find(ws.root, phone.id).key == phone.key


def test_the_lock_file_next_to_the_key_is_owner_only(ws):
    store.pair(ws.root, label="iPhone", addon="tixlike")
    lock = store.path().with_name(store.path().name + ".lock")
    assert lock.exists() and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_permissions_default_and_save(ws):
    # R21: a paired phone is the owner, so every kind is on until the owner switches one off
    assert store.permissions(ws.root) == {"answer": True, "request_changes": True, "approve": True, "verdict": True,
                                          "ticket_request": True}
    store.set_permissions(ws.root, {"approve": True})
    assert store.permissions(ws.root)["approve"] is True and store.permissions(ws.root)["answer"] is False


def test_permissions_never_include_move(ws):
    store.set_permissions(ws.root, {"move": True, "approve": True})
    assert "move" not in store.permissions(ws.root)
    assert store.KINDS == ("answer", "request_changes", "approve", "verdict", "ticket_request")


def test_revoke_keeps_the_entry_but_marks_it(ws):
    phone, _ = store.pair(ws.root, label="iPhone", addon="tixlike")
    store.revoke(ws.root, phone.id)
    assert store.find(ws.root, phone.id).revoked_at is not None


def test_workspaces_are_separate(ws, tmp_path):
    phone, _ = store.pair(ws.root, label="iPhone", addon="tixlike")
    assert store.find(tmp_path, phone.id) is None


def test_pair_link_format():
    assert store.pair_link("https://x.invalid/pair#sp1", "ph_abc", b"\x00" * 32) == \
        "https://x.invalid/pair#sp1.ph_abc." + "A" * 43


def test_check_code_is_the_documented_formula():
    import hashlib
    key = bytes(range(32))
    n = int.from_bytes(hashlib.sha256(b"orch/pair/v1|" + key).digest()[:4], "big") % 1_000_000
    assert store.check_code(key) == f"{n:06d}"


@pytest.mark.parametrize("label", ["", " ", "x" * 41, "<script>", "a\x00b", "ü"])
def test_bad_labels_are_refused(ws, label):
    from orch.errors import OrchError
    with pytest.raises(OrchError):
        store.pair(ws.root, label=label, addon="tixlike")
    assert store.phones(ws.root) == []


def test_a_broken_file_reads_as_no_phones(ws):
    store.path().parent.mkdir(parents=True, exist_ok=True)
    store.path().write_text("{not json", encoding="utf-8")
    assert store.phones(ws.root) == [] and store.permissions(ws.root)["answer"] is True
    phone, _ = store.pair(ws.root, label="iPhone", addon="tixlike")
    assert stat.S_IMODE(store.path().stat().st_mode) == 0o600
    broken = store.path().with_name(store.path().name + ".broken")
    assert broken.exists() and stat.S_IMODE(broken.stat().st_mode) == 0o600


def test_pairing_target_is_https_only():
    assert PairingTarget("https://x.invalid/pair#sp1", "TIX").url.startswith("https://")
    for bad in ("http://x.invalid/pair", "javascript:alert(1)", "https://x.invalid/a b"):
        with pytest.raises(ValueError):
            PairingTarget(bad, "TIX")


class Tixlike:
    def pairing_target(self, view):
        return PairingTarget("https://x.invalid/pair#sp1", "TIX")

    def decisions(self, view):
        return []


@pytest.fixture
def client(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    la = loaded(ws, Tixlike(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                settings_schema=[])
    ws._addons = AddonRegistry(ws, {"tixlike": la})
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    return c


def test_phones_card_pairs_once_and_shows_the_qr_once(client, ws):
    html = client.get("/workspace").text
    assert 'id="phones"' in html and "Pair a phone" in html
    r = client.post("/workspace/phones/pair", data={"addon": "tixlike", "label": "iPhone"}, headers=ORIGIN,
                    follow_redirects=False)
    loc = r.headers["location"]
    phone = store.phones(ws.root)[0]
    key_b64 = store.pair_link("p", "i", phone.key).rsplit(".", 1)[1]
    assert key_b64 not in loc
    first = client.get(loc)
    assert "<svg" in first.text and store.check_code(phone.key) in first.text
    assert first.headers["cache-control"] == "no-store" and "etag" not in first.headers
    assert "Copy pairing link" in first.text and key_b64 in first.text
    assert key_b64 not in client.get(loc).text and key_b64 not in client.get("/workspace").text


def test_a_pair_token_of_the_wrong_kind_is_not_consumed(client, ws):
    """?pair= and an action's Reveal share one one-time store (app.state.reveals); a Reveal token handed to
    ?pair= by mistake (or by someone fishing) must not be burned before its own, legitimate use."""
    reveals = client.app.state.reveals
    token = reveals.put({"kind": "reveal", "label": "x", "text": "shh"})
    html = client.get(f"/workspace?pair={token}").text
    assert "Copy pairing link" not in html and "shh" not in html
    assert reveals.peek(token) == {"kind": "reveal", "label": "x", "text": "shh"}  # still there, not popped


def test_a_pair_token_handed_to_reveal_is_not_consumed(client, ws):
    """The other direction: a pairing token handed to ?reveal= must not be popped (and burned) by a page render."""
    reveals = client.app.state.reveals
    entry = {"kind": "pair", "url": "https://x.invalid/pair#sp1.ph_x.KEY", "label": "iPhone"}
    token = reveals.put(dict(entry))
    html = client.get(f"/workspace?reveal={token}").text
    assert "KEY" not in html
    assert reveals.peek(token) == entry  # still there for its own ?pair= use


def test_pairing_never_logs_the_key(client, ws):
    client.post("/workspace/phones/pair", data={"addon": "tixlike", "label": "iPhone"}, headers=ORIGIN,
                follow_redirects=False)
    key_b64 = store.b64u(store.phones(ws.root)[0].key)
    for p in ws.state_dir.rglob("*"):
        if p.is_file():
            assert key_b64 not in p.read_text(encoding="utf-8", errors="replace"), p


def test_phone_posts_are_same_origin_only(client, ws):
    assert client.post("/workspace/phones/pair", data={"addon": "tixlike", "label": "x"},
                       follow_redirects=False).status_code == 403
    assert store.phones(ws.root) == []
    phone, _ = store.pair(ws.root, label="iPhone", addon="tixlike")
    assert client.post(f"/workspace/phones/{phone.id}/revoke", follow_redirects=False).status_code == 403
    assert store.find(ws.root, phone.id).revoked_at is None
    assert client.post("/workspace/phones/permissions", data={"answer": "1"},
                       follow_redirects=False).status_code == 403
    assert store.permissions(ws.root)["approve"] is True  # unchanged: still the default


def test_phone_posts_need_the_token(ws, client):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    anon = TestClient(create_app(ws, "tok"))
    r = anon.post("/workspace/phones/pair", data={"addon": "tixlike", "label": "x"}, headers=ORIGIN,
                  follow_redirects=False)
    assert r.status_code in (401, 403) and store.phones(ws.root) == []


def test_pair_refuses_an_addon_without_a_pairing_target(client, ws):
    r = client.post("/workspace/phones/pair", data={"addon": "other", "label": "iPhone"}, headers=ORIGIN)
    assert "cannot pair" in r.text and store.phones(ws.root) == []


def test_revoke_and_permissions_from_the_card(client, ws):
    phone, _ = store.pair(ws.root, label="iPhone", addon="tixlike")
    html = client.get("/workspace").text
    assert "iPhone" in html and f"/workspace/phones/{phone.id}/revoke" in html
    assert "Answers apply directly" in html and "Verdicts apply directly" in html
    r = client.post(f"/workspace/phones/{phone.id}/revoke", headers=ORIGIN)
    assert "Phone revoked" in r.text and store.find(ws.root, phone.id).revoked_at is not None
    r = client.post("/workspace/phones/permissions", data={"answer": "1", "approve": "1"}, headers=ORIGIN)
    assert "saved" in r.text
    assert store.permissions(ws.root) == {"answer": True, "request_changes": False, "approve": True, "verdict": False,
                                          "ticket_request": False}


def test_phones_card_hidden_without_a_remote_addon(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    ws._addons = AddonRegistry(ws, {})
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    assert 'id="phones"' not in c.get("/workspace").text


def test_pairing_targets_ask_only_remote_humans_addons(ws):
    from orch.addons.runtime import AddonRuntime
    plain = loaded(ws, Tixlike(), name="plain")
    remote = loaded(ws, Tixlike(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                    settings_schema=[])
    ws._addons = AddonRegistry(ws, {"plain": plain, "tixlike": remote})
    assert [n for n, _ in AddonRuntime(ws).pairing_targets()] == ["tixlike"]


def test_a_failing_pairing_target_is_skipped(ws):
    from orch.addons.runtime import AddonRuntime

    class Broken(Tixlike):
        def pairing_target(self, view):
            raise RuntimeError("boom")
    la = loaded(ws, Broken(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                settings_schema=[])
    ws._addons = AddonRegistry(ws, {"tixlike": la})
    assert AddonRuntime(ws).pairing_targets() == []
