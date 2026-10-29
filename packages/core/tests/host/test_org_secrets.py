"""Company secrets at the host (M3 S-3, #1431-#1433; Opus conditions 10-15 of ``state/devA/secrets-design.md``).

The real served workspace, a TLS fake hub that signs ``hub.org_secrets`` bundles with the hub's op key exactly as it
signs ``hub.org_skills``, and wraps sealed with the real S-1 construction to the workspace's own WXK."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
from types import SimpleNamespace

import pytest

from orch import crypto
from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.host import orgmcp, orgsecrets, orgstate, spaceops
from orch.host.launch import launch_argv
from orch.host.mcpshared import tool_pin, tool_view
from orch.hubclient import orgbundles as OB
from orch.hubclient import orgsecrets as S
from orch.hubclient.errors import HubRefused
from orch.runner.config import parse
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import human, lab, served  # noqa: F401, F811
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.host.test_org_ops import (  # noqa: F401
    OrgHub,
    dev_workspace,
    genesis,
    make_card,
    no_log_sync_sender,
    now_ms,
    req,
    rows,
    svc,
    sync,
)
from tests.hub.tlshub import TlsHub
from tests.test_mcp_proxy import FakeUp

VALUE = "tok-AbCdEf123456-xyz"
SID = "11" * 16
GOOD_SEALED = crypto.b64u(crypto.public_bytes(crypto.generate_private_key()) + b"\0" * 40)  # a real point, any tail
SID2 = "22" * 16
DAY = 24 * 3600 * 1000
URL = "https://vault.example.com/mcp"


class SecretsHub(OrgHub):
    """The hub side of 7.5: a list of wrap entries, a bundle version, and a log of ``secret_use`` calls."""

    def __init__(self, hub: TlsHub, card: dict, me: str) -> None:
        super().__init__(hub, card, me)
        self._secrets: list[dict] = []
        self.secrets_version = 0
        self.issued_ms = 0
        self.uses: list[dict] = []
        self.use_fails = False

    @property
    def secrets(self) -> list[dict]:
        return self._secrets

    @secrets.setter
    def secrets(self, rows: list[dict]) -> None:
        """Every change of content is a new bundle version (same version with other bytes is a conflict)."""
        self._secrets = rows
        self.secrets_version += 1

    def secrets_bundle(self) -> dict:
        o = {"v": 2, "suite": 2, "kind": "hub.org_secrets", "hv": 0, "hub_id": self.hub.hub_id,
             "op_cert_id": self.op_cert_id, "workspace_id": self.me, "version": self.secrets_version,
             "issued_ms": self.issued_ms, "dev": True,
             "secrets": sorted(self.secrets, key=lambda i: i["name"])}  # fmt: skip
        return {"o": o, "sig": crypto.b64u(suite.sign(self.hub.hk_op, OB.SIG_ORG_SECRETS + cj(o)))}

    def handle(self, h: TlsHub, o: dict):
        if o["op"] == "org_secrets_get":
            self.calls.append("secrets")
            if "secrets" in self.refuse:
                return h.reply(o, body={"error": self.refuse["secrets"]}, ts_ms=o["ts_ms"])
            return h.reply(o, body={"bundle": self.secrets_bundle()}, ts_ms=o["ts_ms"])
        if o["op"] == "secret_use":
            self.uses.append(o["body"])
            if self.use_fails:
                return h.reply(o, body={"error": "rate_limited"}, ts_ms=o["ts_ms"])
            return h.reply(o, body={}, ts_ms=o["ts_ms"])
        return super().handle(h, o)


@pytest.fixture
def sorg(human, served, keystore):  # noqa: F811
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    holder: list[SecretsHub] = []
    hub = TlsHub(wsk_pub=wsk, on_request=lambda h, o: holder[0].handle(h, o))
    hub.workspace_id = served.wid
    hub.start()
    card = make_card(hub, now_ms(served))
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    oh = SecretsHub(hub, card, served.wid)
    holder.append(oh)
    args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{hub.port}"}
    assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
    oh.issued_ms = now_ms(served)
    try:
        yield oh
    finally:
        hub.close()


@pytest.fixture(autouse=True)
def clean_memory():
    orgsecrets._CACHE.clear()  # noqa: SLF001 - module state; workspace ids repeat between tests
    orgsecrets._USAGE.clear()  # noqa: SLF001
    yield
    orgsecrets._CACHE.clear()  # noqa: SLF001
    orgsecrets._USAGE.clear()  # noqa: SLF001


@pytest.fixture
def iso(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(orgstate, "isolated", lambda service: state["on"])
    return state


class World:
    def __init__(self, served, org) -> None:  # noqa: F811
        self.served, self.org = served, org
        self.service = svc(served)
        self.wxk, self.wxk_version = spaceops._wxk(self.service)
        self.wxk_pub = crypto.public_bytes(self.wxk)

    def now(self) -> int:
        return now_ms(self.served)

    def advance(self, ms: int) -> None:
        self.served.lab.ws.clock[0] += ms / 1000

    def entry(
        self,
        name="vault-key",
        value=VALUE,
        *,
        sid=SID,
        version=1,
        days=30,
        expires=None,
        wxk_version=None,
        seal_with: dict | None = None,
        check_value=True,
    ) -> dict:
        wv = self.wxk_version if wxk_version is None else wxk_version
        exp = self.now() + days * DAY if expires is None else expires
        seal = {"wxk_pub": self.wxk_pub, "hub_id": self.org.hub.hub_id, "workspace_id": self.served.wid,
                "secret_id": sid, "version": version, "wxk_version": wv, "expires_ms": exp, "name": name,
                "value": value}  # fmt: skip
        seal.update(seal_with or {})
        if check_value:
            sealed = S.seal_org_secret(**seal)
        else:
            sealed = S._seal_with_ephemeral(crypto.generate_private_key(), **seal, check_value=False)  # noqa: SLF001
        return {"id": sid, "name": name, "version": version, "expires_ms": exp, "wxk_version": wv,
                "sealed": crypto.b64u(sealed)}  # fmt: skip

    def accept_server(self, name="vault", secret="vault-key", url=URL):
        srv = self.org.server(name, url=url, auth="hub_secret", secret=secret)
        self.org.servers = [s for s in self.org.servers if s["name"] != name] + [srv]
        self.org.versions["mcp"] += 1
        sync(self.served)
        ok(req(self.served, "org.accept", {"kind": "mcp", "name": name, "digest": srv["digest"]}))
        return srv

    def files(self) -> set[str]:
        d = orgstate.org_dir(self.service)
        return set(os.listdir(d)) if d.exists() else set()

    def stored(self) -> list[dict]:
        return orgsecrets._wraps(self.service, orgstate._state(self.service))[0]  # noqa: SLF001


@pytest.fixture
def w(served, sorg, iso):  # noqa: F811
    return World(served, sorg)


def sec_sync(w):
    return ok(req(w.served, "org.sync"))["data"]["result"]


def get(w, name="vault-key"):
    return orgmcp.org_secret(w.service, name)


# -- the verifier (cond 12) ----------


def test_the_verifier_takes_a_good_bundle_and_refuses_the_rest(w):
    w.org.secrets = [w.entry()]
    held = orgstate._held(w.service, orgstate._state(w.service))  # noqa: SLF001
    st = orgstate._state(w.service)  # noqa: SLF001
    kw = {"workspace_id": w.served.wid, "dev_mode": True, "pinned": None, "revoked_op_certs": frozenset()}
    good = w.org.secrets_bundle()
    o = OB.verify_org_secrets(good, held, **kw)
    assert o["secrets"][0]["name"] == "vault-key" and st.hub_id == o["hub_id"]
    pin = {"version": o["version"], "digest": OB.secrets_digest(o)}
    assert OB.verify_org_secrets(good, held, **{**kw, "pinned": pin}) == o  # an identical re-send
    w.org.secrets_version = 0  # a version the hub never signs
    with pytest.raises(HubRefused) as e:
        OB.verify_org_secrets(w.org.secrets_bundle(), held, **kw)
    assert e.value.code == "malformed"
    w.org.secrets_version = 0
    w.org.secrets = [w.entry(value="another-value-123")]  # version 1 again, other bytes
    with pytest.raises(HubRefused) as e:
        OB.verify_org_secrets(w.org.secrets_bundle(), held, **{**kw, "pinned": pin})
    assert e.value.code == "bundle_conflict"
    with pytest.raises(HubRefused) as e:
        OB.verify_org_secrets(good, held, **{**kw, "pinned": {"version": 2, "digest": pin["digest"]}})
    assert e.value.code == "stale_bundle"
    with pytest.raises(HubRefused) as e:
        OB.verify_org_secrets(good, held, **{**kw, "workspace_id": "ab" * 16})
    assert e.value.code == "other_workspace"
    bad = json.loads(json.dumps(good))
    bad["sig"] = crypto.b64u(b"x" * 64)
    with pytest.raises(HubRefused) as e:
        OB.verify_org_secrets(bad, held, **kw)
    assert e.value.code == "bad_signature"
    skills_label = json.loads(json.dumps(good))  # a bundle of another kind does not verify as secrets
    skills_label["o"]["kind"] = "hub.org_skills"
    with pytest.raises(HubRefused):
        OB.verify_org_secrets(skills_label, held, **kw)


def test_duplicate_names_or_ids_refuse_the_whole_bundle(w):
    a = w.entry("vault-key")
    for dup in (w.entry("vault-key", sid=SID2), {**w.entry("other-key", sid=SID), "name": "other-key"}):
        w.org.secrets = [a, dup]
        held = orgstate._held(w.service, orgstate._state(w.service))  # noqa: SLF001
        with pytest.raises(HubRefused) as e:
            OB.verify_org_secrets(w.org.secrets_bundle(), held, workspace_id=w.served.wid, dev_mode=True, pinned=None)
        assert e.value.code == "malformed"
    # and through the sync: nothing is stored, the earlier state stays
    w.accept_server()
    w.org.secrets = [a, w.entry("vault-key", sid=SID2)]
    assert sec_sync(w)["secrets"] == "malformed" and w.stored() == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(extra=1),
        lambda e: e.update(name="Bad_Name"),
        lambda e: e.update(version=0),
        lambda e: e.update(version=2**32),
        lambda e: e.update(expires_ms=-1),
        lambda e: e.update(wxk_version=-1),
        lambda e: e.update(id="zz" * 16),
        lambda e: e.update(sealed="short"),
        lambda e: e.update(sealed=crypto.b64u(b"\0" * 5000)),
        lambda e: e.update(sealed=None),
        lambda e: e.pop("sealed"),
    ],
)
def test_an_entry_of_the_wrong_shape_refuses_the_bundle(mutate):
    e = {"id": SID, "name": "vault-key", "version": 1, "expires_ms": 5, "wxk_version": 1,
         "sealed": GOOD_SEALED}  # fmt: skip
    assert OB.valid_org_secrets([e])
    mutate(e)
    assert not OB.valid_org_secrets([e])


# -- tier A never asks (cond 11) ----------


def test_tier_a_never_requests_the_wraps_and_keeps_nothing(w, iso):
    iso["on"] = False
    w.org.secrets = [w.entry()]
    got = sec_sync(w)
    assert "secrets" not in got  # nothing was asked, nothing is reported
    assert "secrets" not in w.org.calls and "secrets.json" not in w.files()
    assert get(w) is None


def test_losing_isolation_deletes_the_wraps_and_the_cache_at_the_next_sync_but_keeps_the_pins(w, iso):
    w.accept_server()
    w.org.secrets = [w.entry()]
    assert sec_sync(w)["secrets"] == "new" and get(w) == VALUE
    assert orgsecrets._CACHE  # noqa: SLF001
    iso["on"] = False
    assert get(w) is None and not orgsecrets._CACHE  # per-call recheck drops the cache at once  # noqa: SLF001
    w.org.calls.clear()
    assert "secrets" not in sec_sync(w)
    assert "secrets" not in w.org.calls
    assert "secrets.json" not in w.files() and "secret-pins.json" in w.files()
    iso["on"] = True
    assert get(w) is None  # nothing was kept: the value needs a sync


# -- store: only what a served hub_secret server names (cond 12, 13) ----------


def test_only_wraps_used_by_an_accepted_server_are_kept_0600(w):
    w.org.secrets = [w.entry("vault-key"), w.entry("unused-key", sid=SID2)]
    assert sec_sync(w)["secrets"] == "new"
    assert w.stored() == []  # no server uses either name yet: nothing is stored
    w.accept_server()
    assert sec_sync(w)["secrets"] in ("new", "same")
    assert [r["name"] for r in w.stored()] == ["vault-key"]
    d = orgstate.org_dir(w.service)
    assert os.stat(d / "secrets.json").st_mode & 0o777 == 0o600
    assert os.stat(d / "secret-pins.json").st_mode & 0o777 == 0o600
    assert VALUE not in (d / "secrets.json").read_text()  # sealed on disk
    assert VALUE not in (d / "secret-pins.json").read_text()


def test_a_server_that_is_not_accepted_gets_no_wrap_and_a_declined_one_loses_it(w):
    srv = w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    assert get(w) == VALUE
    ok(req(w.served, "org.decline", {"kind": "mcp", "name": "vault"}))
    assert w.stored() == [] and get(w) is None and "secrets.json" not in w.files()
    assert srv["name"] == "vault"


def test_missing_removed_and_expired_wraps_are_deleted_at_the_sync(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    assert get(w) == VALUE
    w.org.secrets = []  # unshared or removed at the hub
    assert sec_sync(w)["secrets"] == "new"
    assert w.stored() == [] and get(w) is None and "secrets.json" not in w.files()
    w.org.secrets = [w.entry(version=2, expires=w.now() + 5000)]
    sec_sync(w)
    assert get(w) == VALUE
    w.advance(10_000)
    assert get(w) is None  # expired: refused at use ...
    w.org.secrets_version += 1
    sec_sync(w)
    assert w.stored() == []  # ... and deleted at the sync
    w.org.secrets = [w.entry(version=3, expires=w.now() - 1)]  # a bundle that carries an already expired wrap
    sec_sync(w)
    assert w.stored() == []


def test_the_rollback_pin_survives_syncs_and_a_lower_version_is_refused(w):
    w.accept_server()
    w.org.secrets = [w.entry(version=2)]
    sec_sync(w)
    assert get(w) == VALUE
    w.org.secrets = []
    sec_sync(w)  # removed: the wrap goes, the pin stays
    assert get(w) is None
    w.org.secrets = [w.entry(version=1, value="older-value-9999")]  # a replay of an older version
    sec_sync(w)
    assert w.stored() == [] and get(w) is None
    w.org.secrets = [w.entry(version=2)]  # the pinned version itself is fine again
    sec_sync(w)
    assert get(w) == VALUE
    w.org.secrets = [w.entry(version=3, value="newer-value-12345")]
    sec_sync(w)
    assert get(w) == "newer-value-12345"
    w.org.secrets = [w.entry(version=2)]  # now 3 is the pin
    sec_sync(w)
    assert get(w) is None


def test_a_lower_bundle_version_changes_nothing(w):
    w.accept_server()
    w.org.secrets_version = 4
    w.org.secrets = [w.entry()]
    sec_sync(w)
    pinned = w.org.secrets_version
    w.org.secrets = []
    w.org.secrets_version = pinned - 1  # an older bundle than the one the host holds
    assert sec_sync(w)["secrets"] == "stale_bundle"
    assert get(w) == VALUE  # a refused bundle never wipes a working secret


def test_a_refusing_or_old_hub_does_not_wipe_the_wrap(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    w.org.refuse["secrets"] = "unknown_op"
    assert sec_sync(w)["secrets"] == "unknown_op" and get(w) == VALUE


# -- unseal, cache (cond 10) ----------


@pytest.fixture
def opens(monkeypatch):
    calls = []
    real = S.open_org_secret

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(S, "open_org_secret", spy)
    return calls


def test_the_cache_is_60s_keyed_and_never_past_expiry(w, opens):
    w.accept_server()
    w.org.secrets = [w.entry(expires=w.now() + 90_000)]
    sec_sync(w)
    assert get(w) == VALUE and get(w) == VALUE and len(opens) == 1
    (key,) = orgsecrets._CACHE  # noqa: SLF001
    assert key == (w.served.wid, SID, 1, w.wxk_version)
    w.advance(61_000)
    assert get(w) == VALUE and len(opens) == 2  # older than 60 s: opened again
    w.advance(30_000)
    assert get(w) is None  # past expires_ms (91 s): not served from the cache either
    assert not orgsecrets._CACHE  # noqa: SLF001


def test_a_sync_that_changes_the_wraps_clears_the_cache(w, opens):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    get(w)
    assert orgsecrets._CACHE  # noqa: SLF001
    w.org.secrets = [w.entry(version=2, value="rotated-value-001")]
    sec_sync(w)
    assert not orgsecrets._CACHE  # noqa: SLF001
    assert get(w) == "rotated-value-001"


def test_a_value_is_never_written_returned_by_an_op_or_logged(w, caplog):
    w.accept_server()
    w.org.secrets = [w.entry()]
    with caplog.at_level(logging.DEBUG):
        sec_sync(w)
        get(w)
        shown = ok(req(w.served, "org.show", {"kind": "mcp", "name": "vault"}))
        offers = ok(req(w.served, "org.offers"))
    assert VALUE not in json.dumps(shown) and VALUE not in json.dumps(offers)
    assert VALUE not in caplog.text
    for root, _, names in os.walk(orgstate.org_dir(w.service).parent):
        for n in names:
            assert VALUE.encode() not in open(os.path.join(root, n), "rb").read()


# -- negative wraps (cond 15) ----------


def _bad_cases(w):
    other_ws, other_hub = "ab" * 16, "cd" * 16
    return {
        "other_workspace": w.entry(seal_with={"workspace_id": other_ws}),
        "other_hub": w.entry(seal_with={"hub_id": other_hub}),
        "name_swapped": w.entry(seal_with={"name": "other-key"}),
        "other_secret_id": w.entry(seal_with={"secret_id": SID2}),
        "other_version": w.entry(seal_with={"version": 7}),
        "other_expiry": w.entry(seal_with={"expires_ms": w.now() + 5 * DAY}),
        "old_key_version": w.entry(seal_with={"wxk_version": w.wxk_version + 1}),
        "other_key": w.entry(seal_with={"wxk_pub": crypto.public_bytes(crypto.generate_private_key())}),
        "bad_value_del": w.entry(value="\x7fnot-valid", check_value=False),
        "bad_value_newline": w.entry(value="line-one\nline-two-xx", check_value=False),
        "bad_value_edge_space": w.entry(value=" padded-value ", check_value=False),
        "beyond_horizon": w.entry(expires=w.now() + 400 * DAY),
    }


@pytest.mark.parametrize(
    "case",
    ["other_workspace", "other_hub", "name_swapped", "other_secret_id", "other_version", "other_expiry",
     "old_key_version", "other_key", "bad_value_del", "bad_value_newline", "bad_value_edge_space",
     "beyond_horizon"],
)  # fmt: skip
def test_a_wrong_wrap_gives_none_and_leaves_nothing_behind(w, caplog, case):
    w.accept_server()
    w.org.secrets = [_bad_cases(w)[case]]
    with caplog.at_level(logging.DEBUG):
        sec_sync(w)
        assert len(w.stored()) == 1  # it was kept (the hub's word), and it still does not open
        assert get(w) is None
    assert not orgsecrets._CACHE  # noqa: SLF001
    assert VALUE not in caplog.text


def test_a_wrap_for_the_old_workspace_key_after_a_rotation_gives_none_and_needs_the_value_again(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    assert get(w) == VALUE
    assert orgsecrets._CACHE  # the value is cached: the rotation must still win  # noqa: SLF001
    # the workspace's key moves to the next version: the old wrap stops opening
    real = spaceops._wxk
    spaceops._wxk = lambda service: (real(service)[0], w.wxk_version + 1)  # noqa: SLF001
    try:
        assert get(w) is None
        shown = ok(req(w.served, "org.show", {"kind": "mcp", "name": "vault"}))["data"]
        assert shown["secret_status"] == "not_received"
    finally:
        spaceops._wxk = real  # noqa: SLF001


def test_an_expired_wrap_gives_none_a_lower_cached_copy_is_not_used(w):
    w.accept_server()
    w.org.secrets = [w.entry(expires=w.now() + 2000)]
    sec_sync(w)
    assert get(w) == VALUE
    w.advance(3000)
    assert get(w) is None


def test_a_name_not_stored_gives_none(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    assert get(w, "other-key") is None and get(w, "vault-key") == VALUE


def test_a_stored_wrap_of_another_binding_is_not_read(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    path = orgstate.org_dir(w.service) / "secrets.json"
    doc = json.loads(path.read_text())
    doc["binding"]["binding_seq"] += 1
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    orgsecrets.drop_cache(w.service)
    assert get(w) is None


# -- show status (item 7) ----------


def test_show_reports_the_secret_status_without_a_value(w, iso):
    w.accept_server()

    def status():
        return ok(req(w.served, "org.show", {"kind": "mcp", "name": "vault"}))["data"]["secret_status"]

    assert status() == "not_received"
    w.org.secrets = [w.entry(expires=w.now() + 5000)]
    sec_sync(w)
    assert status() == "ready"
    w.advance(6000)
    assert status() == "expired"
    iso["on"] = False
    assert status() == "needs_isolation"
    plain = ok(req(w.served, "org.show", {"kind": "mcp", "name": "vault"}))["data"]
    assert VALUE not in json.dumps(plain) and "sealed" not in json.dumps(plain)
    w.org.servers.append(w.org.server("crm"))
    w.org.versions["mcp"] += 1
    sec_sync(w)
    assert "secret_status" not in ok(req(w.served, "org.show", {"kind": "mcp", "name": "crm"}))["data"]


# -- the accepted digest covers name, url and auth kind (answer 2) ----------


def test_the_digest_of_a_secret_server_changes_with_secret_name_url_and_auth(w):
    base = w.org.server("vault", url=URL, auth="hub_secret", secret="vault-key")
    changed = [
        w.org.server("vault", url=URL, auth="hub_secret", secret="other-key"),
        w.org.server("vault", url="https://evil.example.com/mcp", auth="hub_secret", secret="vault-key"),
        w.org.server("vault", url=URL, auth="none", secret=None),
    ]
    assert len({base["digest"], *(c["digest"] for c in changed)}) == 4
    # and a changed binding really needs a new accept: the accepted entry no longer matches, nothing is served
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    assert [s["name"] for s in orgstate.served(w.service)["mcp"]] == ["vault"]
    w.org.servers = [changed[0]]
    w.org.versions["mcp"] += 1
    sec_sync(w)
    assert orgstate.served(w.service)["mcp"] == []
    assert rows(req(w.served, "org.offers"), "mcp")["vault"]["status"] == "changed"
    assert w.stored() == []  # nothing serves, so no wrap is kept


# -- usage counts (best effort) ----------


def test_use_counts_are_reported_at_sync_and_a_failure_never_blocks(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    for _ in range(3):
        assert get(w) == VALUE  # a cache hit counts too
    w.org.use_fails = True
    assert sec_sync(w)["secrets"] == "same"  # the hub refused secret_use: the sync still works
    assert get(w) == VALUE
    w.org.use_fails = False
    w.org.uses.clear()
    sec_sync(w)
    assert len(w.org.uses) == 1 and set(w.org.uses[0]) == {"secret_id", "day", "n"}
    assert w.org.uses[0]["secret_id"] == SID and w.org.uses[0]["n"] == 4
    sec_sync(w)
    assert len(w.org.uses) == 1  # reported once


# -- the proxy end to end ----------


def post(port, cap, method="tools/list", params=None):
    params = params or ({"name": "echo", "arguments": {}} if method == "tools/call" else None)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
    c.request("POST", "/m/vault", json.dumps(body), {"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {cap}"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data.decode()


@pytest.fixture
def live(w, caplog):
    """An accepted secret server, a synced wrap, a proxy with a fake upstream that records the bearer."""
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    bearers: list = []

    t = {"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}

    def make(rec, bearer):
        bearers.append(bearer)
        up = FakeUp(tools=[t])
        up.call_result = {"content": [{"type": "text", "text": f"you sent Authorization: Bearer {bearer}"}]}
        return up

    hub = w.service.runners
    orgmcp.proxy_for(hub, _upstream=make)
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", parse({"v": 1}), workdir=True)
    got = w.served.h.run(orgmcp.launch_mcp(SimpleNamespace(service=w.service), hub, "r_1", argv))
    assert got is not None and got["servers"] == ["vault"]
    # the owner turns the tool On for the pin the proxy saw
    w.served.h.run(asyncio.to_thread(post, got["port"], got["capability"]))
    pin = tool_pin(tool_view(t)[0])
    ok(req(w.served, "org.tool", {"server": "vault", "tool": "echo", "on": True, "pin": pin}))
    yield SimpleNamespace(w=w, got=got, hub=hub, bearers=bearers, proxy=hub.org_mcp)
    w.served.h.run(hub.org_mcp.close()) if getattr(hub, "org_mcp", None) else None


def call(live, method="tools/call"):
    return live.w.served.h.run(asyncio.to_thread(post, live.got["port"], live.got["capability"], method))


def test_end_to_end_the_upstream_gets_the_bearer_and_the_agent_never_sees_the_value(live, caplog):
    with caplog.at_level(logging.DEBUG):
        status, text = call(live)
    assert status == 200
    assert live.bearers == [VALUE]  # Authorization: Bearer <value> is built from this
    assert VALUE not in text and "[redacted]" in text
    assert VALUE not in caplog.text
    assert live.proxy._held_now == (VALUE,)  # noqa: SLF001


def test_a_rotation_at_sync_replaces_the_held_value_and_a_removal_drops_it(live):
    w = live.w
    assert call(live)[0] == 200
    w.org.secrets = [w.entry(version=2, value="rotated-value-777")]
    sec_sync(w)
    status, text = call(live)
    assert status == 200 and live.bearers[-1] == "rotated-value-777"
    assert live.proxy._held_now == ("rotated-value-777",)  # noqa: SLF001
    w.org.secrets = []
    sec_sync(w)
    assert call(live)[0] == 503
    assert live.proxy._held_now == ()  # noqa: SLF001


def test_losing_isolation_stops_the_server_and_drops_the_held_value(live, iso):
    assert call(live)[0] == 200
    iso["on"] = False
    live.proxy._iso.clear()  # noqa: SLF001 - the proxy's own 60 s isolation cache
    assert call(live)[0] in (403, 503)
    assert live.proxy._held_now == ()  # noqa: SLF001
    assert not orgsecrets._CACHE  # noqa: SLF001


# -- unbind and leave: revoke -> drop cache and held -> purge (cond 14) ----------


def test_unbind_revokes_then_drops_values_then_purges(live, human, monkeypatch):  # noqa: F811
    w = live.w
    assert call(live)[0] == 200 and orgsecrets._CACHE and live.proxy._held_now  # noqa: SLF001
    order: list = []
    real_purge, real_drop = orgstate.purge, orgsecrets.drop_cache

    def drop(service):
        order.append(("drop_cache", bool(live.proxy._held_now), bool(live.proxy._caps)))  # noqa: SLF001
        real_drop(service)

    def purge(service):
        order.append(("purge", bool(orgsecrets._CACHE), orgstate.org_dir(service).exists()))  # noqa: SLF001
        real_purge(service)

    monkeypatch.setattr(orgsecrets, "drop_cache", drop)
    monkeypatch.setattr(orgstate, "purge", purge)
    human.run("hub.leave", {})
    ok(w.served.client().req("hub.unbind", {}, idem=True))
    assert order[0] == ("drop_cache", False, False)  # capabilities revoked and their held values gone BEFORE
    assert order[1] == ("purge", False, True)  # the cache was already empty, the folder still there
    assert not orgstate.org_dir(w.service).exists()
    assert not live.proxy._held_now and not orgsecrets._CACHE  # noqa: SLF001
    assert call(live)[0] == 401
    assert get(w) is None


# -- review M1: a mode switch (same hub, binding_seq + 1) does not reset the rollback pins ------------------------


@pytest.fixture
def switched(w, monkeypatch):
    """After this runs the binding is the same hub with binding_seq + 1 (what hub.connect kind=switch gives)."""
    real = orgstate._binding  # noqa: SLF001
    seq = {"add": 0}
    monkeypatch.setattr(
        orgstate, "_binding", lambda st: {**real(st), "binding_seq": real(st)["binding_seq"] + seq["add"]}
    )

    def switch():
        seq["add"] = 1

    return switch


def test_a_switch_keeps_the_pins_so_a_lower_secret_version_is_still_refused(w, switched):
    w.accept_server()
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    assert get(w) == VALUE
    switched()
    w.org.secrets = [w.entry(version=2, value="older-value-2222")]  # the hub serves an older secret after the switch
    w.accept_server()  # its own sync is the first one of the new binding; the new binding needs its own accept
    sec_sync(w)
    assert w.stored() == [] and get(w) is None
    pins = json.loads((orgstate.org_dir(w.service) / "secret-pins.json").read_text())
    assert pins["ids"][SID] == 3


def test_a_switch_keeps_the_bundle_version_pin(w, switched):
    w.accept_server()
    w.org.secrets_version = 4
    w.org.secrets = [w.entry()]
    sec_sync(w)
    pinned = w.org.secrets_version
    switched()
    w.org.secrets_version = pinned - 1  # an older bundle than the one pinned before the switch
    w.accept_server()
    assert sec_sync(w)["secrets"] == "stale_bundle"


# -- review L3: the pin file is bounded and fails closed -----------------------------------------------------------


def test_a_bundle_beyond_the_pin_cap_is_refused(w, monkeypatch):
    monkeypatch.setattr(orgsecrets, "PINS_MAX", 2)
    w.accept_server()
    w.org.secrets = [w.entry("vault-key", sid=SID), w.entry("aux-key", sid=SID2)]
    assert sec_sync(w)["secrets"] == "new"
    w.org.secrets = [w.entry("vault-key", sid=SID), w.entry("aux-key", sid=SID2), w.entry("third-key", sid="33" * 16)]
    assert sec_sync(w)["secrets"] == "too_many_secrets"
    assert get(w) == VALUE  # a refused bundle changes nothing


@pytest.mark.parametrize(
    "bad",
    ["[]", '{"hub_id": "x", "ids": []}', '{"ids": {"a": "b"}}', '{"ids": {"a": 0}}', '{"ids": {"a": 1}, "bundle": 5}'],
)
def test_an_unusable_pin_file_refuses_the_sync_instead_of_reading_as_empty(w, bad):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    path = orgstate.org_dir(w.service) / "secret-pins.json"
    path.write_text(bad)
    os.chmod(path, 0o600)
    w.org.secrets = [w.entry(version=2)]
    assert sec_sync(w)["secrets"] == "pins_unusable"


def test_an_oversize_pin_file_refuses_the_sync(w):
    w.accept_server()
    w.org.secrets = [w.entry()]
    sec_sync(w)
    path = orgstate.org_dir(w.service) / "secret-pins.json"
    doc = json.loads(path.read_text())
    doc["ids"] = {f"{i:032x}": 1 for i in range(orgsecrets.PINS_MAX + 1)}
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    w.org.secrets = [w.entry(version=2)]
    assert sec_sync(w)["secrets"] == "pins_unusable"


# -- review L2: held values go at the sync, not at the next call -----------------------------------------------------


def test_a_rotation_or_removal_at_sync_empties_the_held_values_at_once(live):
    w = live.w
    assert call(live)[0] == 200 and live.proxy._held_now == (VALUE,)  # noqa: SLF001
    w.org.secrets = [w.entry(version=2, value="rotated-value-777")]
    sec_sync(w)
    assert live.proxy._held_now == ()  # noqa: SLF001 - no further call was needed
    assert call(live)[0] == 200 and live.proxy._held_now == ("rotated-value-777",)  # noqa: SLF001
    w.org.secrets = []
    sec_sync(w)
    assert live.proxy._held_now == ()  # noqa: SLF001
    assert len(live.proxy._caps) == 1  # the capability itself lives on


def test_an_expiry_found_at_sync_and_a_decline_empty_the_held_values(live):
    w = live.w
    w.org.secrets = [w.entry(version=2, expires=w.now() + 5000)]
    sec_sync(w)
    call(live)
    assert live.proxy._held_now  # noqa: SLF001
    w.advance(6000)
    w.org.secrets = [w.entry(version=3, expires=w.now() + 5000)]
    w.org.secrets = []
    sec_sync(w)
    assert live.proxy._held_now == ()  # noqa: SLF001
    w.org.secrets = [w.entry(version=4)]
    sec_sync(w)
    call(live)
    assert live.proxy._held_now  # noqa: SLF001
    ok(req(w.served, "org.decline", {"kind": "mcp", "name": "vault"}))
    assert live.proxy._held_now == ()  # noqa: SLF001


# -- review M1b: a pin file without a usable hub_id is not "another hub" ----------------------------------------------


@pytest.mark.parametrize("hub_id", [None, 5, ["x"], ""])
def test_pins_without_a_usable_hub_id_are_unusable_never_empty(w, hub_id):
    w.accept_server()
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    path = orgstate.org_dir(w.service) / "secret-pins.json"
    doc = json.loads(path.read_text())
    if hub_id is None:
        doc.pop("hub_id")
    else:
        doc["hub_id"] = hub_id
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    with pytest.raises(orgsecrets.PinsUnusable):
        orgsecrets._pins(w.service, orgstate._state(w.service))  # noqa: SLF001
    w.org.secrets = [w.entry(version=2, value="older-value-2222")]
    assert sec_sync(w)["secrets"] == "pins_unusable"
    assert json.loads(path.read_text()).get("ids", {}).get(SID) == 3  # nothing was reset


def test_pins_of_another_hub_are_empty_pins(w):
    w.accept_server()
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    path = orgstate.org_dir(w.service) / "secret-pins.json"
    doc = json.loads(path.read_text())
    doc["hub_id"] = "ab" * 16
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    assert orgsecrets._pins(w.service, orgstate._state(w.service))["ids"] == {}  # noqa: SLF001


# -- review delta LOW 1: the pin file is read without following links ----------


def _pins_path(w):
    return orgstate.org_dir(w.service) / "secret-pins.json"


def test_a_dangling_symlink_for_the_pin_file_fails_closed(w):
    w.accept_server()
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    path = _pins_path(w)
    path.unlink()
    path.symlink_to(path.parent / "nowhere.json")
    w.org.secrets = [w.entry(version=2, value="older-value-2222")]
    assert sec_sync(w)["secrets"] == "pins_unusable"
    assert path.is_symlink()  # nothing replaced it


def test_a_pin_file_with_an_unsafe_mode_fails_closed(w):
    w.accept_server()
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    os.chmod(_pins_path(w), 0o644)
    w.org.secrets = [w.entry(version=4)]
    assert sec_sync(w)["secrets"] == "pins_unusable"


# -- review delta LOW 2: values are forgotten AFTER the file changed ----------


def test_the_cache_and_held_values_are_dropped_after_the_files_changed(live, monkeypatch):
    w = live.w
    seen: list[tuple[str, bool]] = []
    real_drop, real_forget = orgsecrets.drop_cache, orgsecrets._forget_held  # noqa: SLF001
    path = orgstate.org_dir(w.service) / "secrets.json"

    def drop(service):
        seen.append(("drop", path.exists() and VALUE in json.dumps(w.stored())))
        real_drop(service)

    def forget(service):
        seen.append(("forget", path.exists() and VALUE in json.dumps(w.stored())))
        real_forget(service)

    monkeypatch.setattr(orgsecrets, "drop_cache", drop)
    monkeypatch.setattr(orgsecrets, "_forget_held", forget)
    old_sealed = w.stored()[0]["sealed"]
    w.org.secrets = [w.entry(version=2, value="rotated-value-777")]
    sec_sync(w)  # rotation: the new wrap is on disk before anything is forgotten
    assert seen and all(w.stored()[0]["sealed"] != old_sealed and not flag for _, flag in seen)
    seen.clear()
    w.org.secrets = []
    sec_sync(w)  # removal: the file is gone before anything is forgotten
    assert seen and all(not path.exists() for _ in seen)
    seen2: list[bool] = []
    monkeypatch.setattr(orgsecrets, "_forget_held", lambda service: seen2.append(path.exists()))
    w.org.secrets = [w.entry(version=3)]
    sec_sync(w)
    ok(req(w.served, "org.decline", {"kind": "mcp", "name": "vault"}))  # prune
    assert seen2 and not seen2[-1]
