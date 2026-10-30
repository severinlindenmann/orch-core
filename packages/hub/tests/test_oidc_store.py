import json
import os
import stat

import pytest

from orch_hub.oidc import OidcError
from orch_hub.oidc.http import MAX_BYTES, UrlHttp
from orch_hub.oidc.store import OidcStore, validate

TENANT = "11111111-2222-3333-4444-555555555555"
GOOD = {
    "v": 1, "provider": "entra", "client_id": "cid", "tenant": TENANT, "domain": "",
    "redirect_uri": "https://hub.example/oidc/callback", "mode": "invited", "idp_mfa": True, "enabled": False, "tested": False,
}  # fmt: skip


@pytest.fixture
def st(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    s = OidcStore(d)
    yield s
    s.close()


def test_config_round_trip_and_modes(st):
    assert st.load() is None
    st.save(GOOD)
    assert st.load() == GOOD
    for f in ("oidc.json", "oidc.db"):
        assert stat.S_IMODE(os.stat(st.state / f).st_mode) == 0o600


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "extra": 1},
        {**GOOD, "client_secret": "x"},  # a secret cannot ride along in the settings file
        {**GOOD, "mode": "everyone"},
        {**GOOD, "enabled": "yes"},
        {**GOOD, "tenant": "common"},
        {**GOOD, "tenant": "9188040d-6c67-4c5b-b112-36a304b66dad"},
        {**GOOD, "redirect_uri": "http://10.0.0.5:8100/oidc/callback"},
    ],
)
def test_bad_config_refused(bad):
    with pytest.raises(OidcError):
        validate(bad)


def test_secret_never_in_settings_and_mode_0600(st):
    st.set_secret("s3cr3t-value-123")
    st.save(GOOD)
    assert "s3cr3t" not in (st.state / "oidc.json").read_text()
    assert stat.S_IMODE(os.stat(st.state / "oidc.secret").st_mode) == 0o600
    assert st.secret() == "s3cr3t-value-123"
    for bad in ("", "short", " padded-secret ", "new\nline-secret"):
        with pytest.raises(OidcError):
            st.set_secret(bad)


def test_cookie_key_is_random_32_bytes_and_stable(st):
    k = st.cookie_key()
    assert len(k) == 32 and st.cookie_key() == k
    assert k != (st.secret() or b"")
    assert stat.S_IMODE(os.stat(st.state / "oidc.key").st_mode) == 0o600


def test_loose_file_is_refused(st):
    st.save(GOOD)
    os.chmod(st.state / "oidc.json", 0o644)
    with pytest.raises(OidcError):
        st.load()


def test_links_keyed_on_subject(st):
    st.link("u1", "entra", TENANT, "oid-1")
    assert st.user_for("entra", TENANT, "oid-1") == "u1"
    assert st.user_for("entra", TENANT, "oid-2") is None
    with pytest.raises(OidcError):  # an identity belongs to one person
        st.link("u2", "entra", TENANT, "oid-1")
    with pytest.raises(OidcError):  # a person has one identity
        st.link("u1", "entra", TENANT, "oid-9")
    assert st.linked("u1")["realm"] == TENANT
    assert st.unlink("u1") and not st.unlink("u1")
    st.link("u1", "entra", TENANT, "oid-9")
    st.unlink_all()
    assert st.linked("u1") is None


def test_http_refuses_plain_http_and_caps_answers(monkeypatch):
    h = UrlHttp()
    with pytest.raises(OidcError):
        h.get_json("http://idp.example/x")
    import io

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(h._opener, "open", lambda req, timeout: R(b"{" + b" " * (MAX_BYTES + 5) + b"}"))
    with pytest.raises(OidcError) as e:
        h.get_json("https://idp.example/x")
    assert e.value.code == "idp_answer_too_big"
    monkeypatch.setattr(h._opener, "open", lambda req, timeout: R(json.dumps({"a": 1}).encode()))
    assert h.get_json("https://idp.example/x") == {"a": 1}
