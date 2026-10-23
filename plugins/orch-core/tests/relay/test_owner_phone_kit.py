"""Self-test of the dev-pair clients (``tests/relay/owner.py``, ``tests/relay/phone.py``) against the in-process mocks:
the owner against a loopback fixture relay (account, primary certificate, the queue steps, the sandbox refusals), the
phone against the pair engine (pending, SAS, challenge, ``cert_code``, ``approved``), then against the bridge (join,
column-R ``whoami``, one ``answer`` write). The real relay is the T2 test's."""

from __future__ import annotations

import json
import os

import pytest

from orch import canon, crypto, devsandbox
from orch.crypto.labels import L
from orch.identity import certreq, certs, memberlist, pairing, reader
from orch.identity import recovery as rec
from orch.identity.pairdevice import cert_digest
from orch.net import http as rhttp
from orch.relay.keys import Member
from orch.wsproto import wire

from . import bridgekit, corpus
from .helpers import OPEN_CONNS, V_ORIGIN, WS, WSK_PRIV
from .mock import MockRelay, Reply, json_reply
from .owner import OwnerRefusedError, SandboxOwner
from .pairkit import OWNER, OWNER_PK, T0, Kit, make_cert, make_challenge
from .phone import DevPhone, PhoneError, write_code

FAST = 2**10


# -- the owner --------------------------------------------------------------------------------------------------------


def make_root(tmp_path, *, person: bool = True, name: str = "sbx"):
    """A sandbox root the way ``dev-pair up`` leaves it after ``orch init``: manifest (with the person), ``secrets/``
    with the recovery code. Returns ``(root, code, person_id)``."""
    root = tmp_path / name
    os.mkdir(root, 0o700)
    code = rec.generate_recovery_code()
    pid = crypto.person_id(crypto.public_bytes(rec._key_at(code, FAST))).hex()
    devsandbox.write_manifest(root, "ab" * 8, person_id=("p_" + pid) if person else None)
    os.mkdir(root / "secrets", 0o700)
    fd = os.open(root / "secrets" / "recovery-code", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, code.encode())
    os.close(fd)
    return root, code, pid


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    for c in OPEN_CONNS:
        c.close()
    OPEN_CONNS.clear()
    m.close()


def owner_for(root, relay):
    conn = rhttp.RelayConn(relay.origin())
    OPEN_CONNS.append(conn)
    return SandboxOwner(root, V_ORIGIN, transport=conn, kdf_n=FAST, now_ms=lambda: T0)


def test_owner_refuses_without_manifest_or_person(tmp_path, relay):
    root, _, _ = make_root(tmp_path)
    os.unlink(root / ".orch-dev-sandbox")
    with pytest.raises(OwnerRefusedError) as e:
        owner_for(root, relay)
    assert e.value.code == "sandbox"
    root2, _, _ = make_root(tmp_path, person=False, name="sbx2")
    with pytest.raises(OwnerRefusedError, match="no sandbox person"):
        owner_for(root2, relay)


def test_owner_refuses_loose_secrets_dir_and_keeps_its_device_keys(tmp_path, relay):
    root, _, _ = make_root(tmp_path)
    first = owner_for(root, relay)
    dev = first.keys.device_id
    assert owner_for(root, relay).keys.device_id == dev  # made once, kept
    for name in ("primary-sig.key", "primary-kx.key"):
        assert (os.stat(root / "secrets" / name).st_mode & 0o777) == 0o600
    os.chmod(root / "secrets", 0o750)
    with pytest.raises(OwnerRefusedError):
        owner_for(root, relay)


class AccountRelay:
    def __init__(self, relay: MockRelay) -> None:
        self.bodies: list[dict] = []
        self.challenge = os.urandom(32)
        relay.fallback = self

    def __call__(self, s):
        if s.path == "/v2/session/challenge":
            return json_reply(200, {"challenge": crypto.b64u(self.challenge), "expires_ms": 1})
        if s.path == "/v2/accounts":
            body = json.loads(s.body)
            self.bodies.append(body)
            return json_reply(200, {"person_id": crypto.person_id(crypto.unb64u(body["pk_pub"])).hex()})
        return json_reply(404, {"error": "not_found"})


def test_owner_creates_the_account_with_a_pk_signed_primary_certificate(tmp_path, relay):
    root, _, pid = make_root(tmp_path)
    fake = AccountRelay(relay)
    owner = owner_for(root, relay)
    path = owner.create_account("INVITECODE0123456789ABCDEFGH")
    body = fake.bodies[0]
    pk = crypto.unb64u(body["pk_pub"])
    assert crypto.person_id(pk).hex() == pid
    cert = body["device_cert"]
    from orch.identity import certs

    o = certs.verify_cert(cert, pk, T0)
    assert o["device_id"] == owner.keys.device_id.hex() and o["scopes_max"] == ["look"]
    proof = wire.auth_bytes(V_ORIGIN, fake.challenge, "device", owner.keys.device_id.hex())
    assert crypto.verify(owner.keys.dk_sig_pub, crypto.unb64u(body["sig"]), proof)
    assert json.loads(path.read_bytes()) == cert and (os.stat(path).st_mode & 0o777) == 0o600
    assert path == root / "primary-cert.json"


def test_the_primary_device_key_signs_logins_only(tmp_path, relay):
    root, _, _ = make_root(tmp_path)
    owner = owner_for(root, relay)
    with pytest.raises(OwnerRefusedError):
        owner.keys.sign(L["sig_device_cert"] + b"{}")


def test_the_primary_reader_decl_is_level_one_and_verifies(tmp_path, relay):
    root, _, pid = make_root(tmp_path)
    owner = owner_for(root, relay)
    decl = owner.reader_decl(WS)
    got = reader.verify_decl(
        decl, workspace_id=WS, dk_sig_pub=owner.keys.dk_sig_pub, dk_kx_pub=owner.keys.dk_kx_pub,
        who={"person_id": pid}, bind={"via": "enrol"}, surfaces=("log",), now_ms=decl["o"]["created_ms"],
    )  # fmt: skip
    assert got.level == 1


# -- the FIFO ---------------------------------------------------------------------------------------------------------


def test_write_code_writes_one_bound_line_to_a_fifo_and_refuses_a_file(tmp_path):
    fifo = tmp_path / "codes"
    os.mkfifo(fifo, 0o600)
    rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        write_code(fifo, "ab" * 16, "sas", "K7M2QX")
        assert os.read(rfd, 100) == b"abababababababababababababababab sas K7M2QX\n"
    finally:
        os.close(rfd)
    plain = tmp_path / "plain"
    plain.write_bytes(b"")
    with pytest.raises((ValueError, OSError)):
        write_code(plain, "ab" * 16, "sas", "K7M2QX", timeout=0.2)
    with pytest.raises(ValueError):
        write_code(fifo, "ab" * 16, "other", "K7M2QX")
    assert plain.read_bytes() == b""


# -- the phone against the pair engine --------------------------------------------------------------------------------


class EngineNet:
    """Pairing routes backed by the in-process engine: a post is ``engine.receive``; the answers are what the fake relay
    recorded for the rid."""

    def __init__(self, kit: Kit) -> None:
        self.kit = kit

    def pair_post(self, offer_id, rid, envelope):
        self.kit.engine.receive(envelope, rid)

    def pair_answers(self, offer_id, rid, wait_s):
        return [env for _, r, env in self.kit.relay.answers if r == rid][-1:]


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    yield k
    k.close()


def drive_host(kit: Kit, phone: DevPhone, state: dict, offer_id: str):
    """One step of the host and the person, called between the phone's polls: type the SAS, answer the primary's
    request with the owner's challenge, compare ``cert_code``, sign, allocate and publish."""
    e = kit.engine
    rec_ = kit.rec(offer_id)
    if rec_["state"] == "pending" and phone.a.sas:
        sas = pairing.sas(WS, offer_id, phone.sig_pub, phone.kx_pub, phone.a.wsk_pub, crypto.unb64u(rec_["sas_nonce"]))
        assert sas == phone.a.sas  # the phone shows what the host would compare
        assert e.answer(offer_id, sas.lower(), True)[0] == "cert_pending"
    elif rec_["state"] == "cert_pending" and "challenge" not in state and kit.relay.requests:
        req = kit.relay.requests[-1]
        ch = make_challenge(req["o"], phone)
        state["challenge"] = ch
        kit.relay.request_state = {"state": "challenged", "challenge": ch, "cert": None, "rejection": None}
        kit.clock.ms += 10_000
        e.tick()
    elif rec_["state"] == "cert_pending" and phone.a.cert_code and "cert" not in state:
        assert phone.a.cert_code == pairing.cert_code(state["challenge"]["o"])
        state["cert"] = make_cert(phone)
        kit.relay.request_state = {"state": "signed", "challenge": state["challenge"], "cert": state["cert"],
                                   "rejection": None}  # fmt: skip
        kit.clock.ms += 10_000
        e.tick()
        e.facts(offer_id)
        a = {
            "offer_id": offer_id, "attempt": kit.rec(offer_id)["consumed"]["attempt"], "device": "d_" + phone.device_id,
            "cert_digest": cert_digest(state["cert"]),
            "scopes": tuple(kit.rec(offer_id)["scopes"]), "review_digest": "sha256:" + "11" * 32,
            "event_id": "01JAAAAAAAAAAAAAAAAAAAAAAA", "event_digest": "sha256:" + "22" * 32, "decl_digest": None,
        }  # fmt: skip
        e.precommit(a, landed=lambda i: False)
        e.committed(a)
        kit.runner.run()
        kit.membership.events.on_committed(offer_id, {"list_seq": 4, "epoch": 3, "adds": [phone.device_id]})
        kit.runner.run()


def test_the_phone_pairs_through_the_engine_and_declares_level_one(kit):
    h = kit.engine.offer(("look", "decide"))
    phone = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)
    state: dict = {}
    seen: list[tuple[str, str, str]] = []
    ap = phone.pair(
        EngineNet(kit), codes=lambda o, ph, c: seen.append((o, ph, c)), deadline_s=30, poll_s=0.0,
        sleep=lambda s: drive_host(kit, phone, state, h.offer_id),
    )  # fmt: skip
    assert [(o, ph) for o, ph, _ in seen] == [(h.offer_id, "sas"), (h.offer_id, "cert_code")]
    assert ap.scopes == ("look", "decide") and (ap.epoch, ap.list_seq) == (3, 4) and ap.pk_pub == OWNER_PK
    assert ap.cert == state["cert"]
    # the host's gate saw a level-1 reader_decl bound to this offer and attempt, who = the link's pk_pin
    assert kit.host.gates[0][0] == h.offer_id
    decl = phone.reader_decl()
    reader.verify_decl(
        decl, workspace_id=WS, dk_sig_pub=phone.sig_pub, dk_kx_pub=phone.kx_pub,
        who={"pk_pin": crypto.b64u(phone.pk_pin)},
        bind={"via": "pair", "offer_id": h.offer_id, "attempt": phone.attempt},
        surfaces=("view",), now_ms=decl["o"]["created_ms"],
    )  # fmt: skip


def test_the_phone_refuses_a_challenge_for_another_key_with_the_alarm(kit):
    h = kit.engine.offer()
    phone = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)
    net = EngineNet(kit)
    env, rid = phone.envelope(phone.meta(), ts=kit.clock.ms)
    net.pair_post(h.offer_id, rid, env)
    phone.check(net.pair_answers(h.offer_id, rid, 1)[0])
    assert kit.engine.answer(h.offer_id, phone.a.sas.lower(), True)[0] == "cert_pending"
    other = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)  # someone else's keys
    ch = make_challenge(kit.relay.requests[-1]["o"], other)
    with pytest.raises(PhoneError) as e:
        phone.check_challenge(ch, OWNER_PK)
    assert e.value.code == "not_my_key" and e.value.alarm


def test_the_phone_drops_an_answer_with_another_key_echoed(kit):
    h = kit.engine.offer()
    mine = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)
    net = EngineNet(kit)
    env, rid = mine.envelope(mine.meta(), ts=kit.clock.ms)
    net.pair_post(h.offer_id, rid, env)
    answer = net.pair_answers(h.offer_id, rid, 1)[0]
    thief = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)  # a second key on the same link
    with pytest.raises(PhoneError):  # not its key: the answer is not sealed to it or echoes another key
        thief.check(answer)


# -- the phone against the bridge ---------------------------------------------------------------------------------


class BridgeNet:
    """Login, member list, grants and the bridge from the bridge kit's in-process world."""

    def __init__(self, world: bridgekit.World, phone: DevPhone, epoch: int, wk: bytes) -> None:
        self.w, self.phone, self.epoch, self.wk = world, phone, epoch, wk
        self.chunks: dict[str, list[bytes]] = {}
        self.logged_in = False

    def login(self, device_id, sign):
        assert device_id == self.phone.device_id
        wire.auth_bytes(V_ORIGIN, b"\0" * 32, "device", device_id)  # the real login signs this layout
        sign(wire.auth_bytes(V_ORIGIN, b"\0" * 32, "device", device_id))
        self.logged_in = True

    def members(self):
        o = memberlist.member_list_o(
            WS, self.epoch, 4, T0, pairing.wk_commit(WS, self.epoch, self.wk),
            [{"device_id": self.phone.device_id, "person_id": OWNER, "scopes": ["look", "decide"]}],
            owner_person_id=OWNER, first=False,
        )  # fmt: skip
        return {"o": o, "sig": crypto.b64u(crypto.sign(WSK_PRIV, L["sig_member_list"] + canon.dumps(o)))}

    def grants(self):
        sealed = memberlist.seal_wk(self.phone.kx_pub, self.phone.device_id, WS, self.epoch, self.wk)
        o = memberlist.wk_grant_o(WS, self.phone.device_id, self.epoch, sealed)
        return [{"o": o, "sig": crypto.b64u(crypto.sign(WSK_PRIV, L["sig_wk_grant"] + canon.dumps(o)))}]

    def bridge_post(self, rid, envelope):
        assert self.logged_in
        self.chunks[rid] = self.w.send(envelope)

    def bridge_answers(self, rid, wait_s):
        return self.chunks.get(rid, [])


def test_the_phone_joins_reads_and_writes_through_the_bridge(kit, tmp_path):
    h = kit.engine.offer(("look", "decide"))
    phone = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)
    state: dict = {}
    phone.pair(EngineNet(kit), codes=lambda *a: None, deadline_s=30, poll_s=0.0,
               sleep=lambda s: drive_host(kit, phone, state, h.offer_id))  # fmt: skip
    w = bridgekit.World(tmp_path)
    try:
        w.epochs.cur = 3
        wk3 = w.epochs.keys[3] = os.urandom(32)
        w.clock.ms = kit.clock.ms
        phone._now = lambda: w.clock.ms
        w.r6.members[phone.device_id] = Member(
            phone.device_id, OWNER, ("look", "decide"), phone.approved.cert, OWNER_PK
        )  # fmt: skip
        net = BridgeNet(w, phone, 3, wk3)
        assert phone.join(net) == {3: wk3} and phone.scopes == ("look", "decide")
        meta, data = phone.whoami(net)
        assert meta == {"status": "ok"} and data["data"]["op"] == "whoami"
        assert w.host.reads == ["whoami"]
        log = bridgekit.ulid(w.clock.ms)
        meta, data = phone.answer(net, log, "Q1", "sha256:" + "0" * 64, "y", based_on="sha256:" + "1" * 64)
        assert meta["status"] == "ok" and data["data"]["seqs"] and w.host.runs[-1][0] == "answer"
    finally:
        w.close()


def test_join_refuses_a_list_below_the_anchor_and_a_wrong_commitment(kit, tmp_path):
    h = kit.engine.offer(("look", "decide"))
    phone = DevPhone(h.link.native, now_ms=lambda: kit.clock.ms)
    state: dict = {}
    phone.pair(EngineNet(kit), codes=lambda *a: None, deadline_s=30, poll_s=0.0,
               sleep=lambda s: drive_host(kit, phone, state, h.offer_id))  # fmt: skip
    w = bridgekit.World(tmp_path)
    try:
        below = BridgeNet(w, phone, 2, os.urandom(32))  # epoch 2 < the anchor's 3
        with pytest.raises(PhoneError) as e:
            phone.join(below)
        assert e.value.code == "below_anchor"
        wk = os.urandom(32)
        bad = BridgeNet(w, phone, 3, wk)
        bad.members = lambda: BridgeNet.members(BridgeNet(w, phone, 3, os.urandom(32)))  # another WK's commitment
        with pytest.raises(PhoneError) as e2:
            phone.join(bad)
        assert e2.value.code == "wk_commit"
    finally:
        w.close()


# -- the owner's queue against the phone -------------------------------------------------------------------------------


class QueueRelay:
    """The relay's side of the primary's routes: one open request, the posted challenge or
    rejection, the device certificate."""

    def __init__(self, relay: MockRelay, request: dict) -> None:
        self.request, self.posted, self.devices = request, {}, {}
        relay.fallback = self

    def __call__(self, s):
        if s.path == "/v2/session/challenge":
            return json_reply(200, {"challenge": crypto.b64u(os.urandom(32)), "expires_ms": 1})
        if s.path == "/v2/session/login":
            return json_reply(200, {"token": "FAKE-primary", "expires_ms": 1})
        if s.path.endswith("/cert-requests") and s.method == "GET":
            return Reply(200, body=json.dumps({"requests": [self.request]}).encode())
        if s.path.startswith("/v2/cert-requests/"):
            self.posted[s.path.rsplit("/", 1)[1]] = json.loads(s.body)
            return json_reply(200, {})
        if s.method == "PUT" and s.path.startswith("/v2/devices/"):
            cert = json.loads(s.body)["cert"]
            self.devices[cert["o"]["device_id"]] = cert
            return json_reply(200, {"device_id": cert["o"]["device_id"]})
        return json_reply(404, {"error": "not_found"})


def sandbox_with_card(tmp_path, relay):
    """A sandbox whose person owns the corpus workspace A: the host's stored card (the corpus card re-delegated by the
    sandbox person) under ``state/hosts/<ws>/membership/cards``."""
    root, code, pid = make_root(tmp_path)
    key = rec._key_at(code, FAST)
    base = corpus.S2["card"]["card"]
    deleg = {**base["delegation"]["o"], "owner_person_id": pid}
    delegation = {"o": deleg, "sig": crypto.b64u(crypto.sign(key, L["sig_ws_delegation"] + canon.dumps(deleg)))}
    o = {**base["o"], "owner_person_id": pid, "relay_url": V_ORIGIN}
    sig = crypto.b64u(crypto.sign(WSK_PRIV, L["sig_card_wsk"] + canon.dumps(o)))
    cards = root / "state" / "hosts" / WS / "membership" / "cards"
    cards.mkdir(parents=True, mode=0o700)
    (cards / "card-1.json").write_bytes(
        canon.dumps({"delegation": delegation, "o": o, "sig": sig, "sealed": base["sealed"]})
    )
    return root, pid, crypto.public_bytes(key)


def cert_request_for(owner: SandboxOwner, phone: DevPhone, pid: str):
    o = certreq.cert_request_o(
        request_id=os.urandom(16).hex(), workspace_id=WS, person_id=pid, primary_device_id=owner.keys.device_id.hex(),
        primary_kx_pub=owner.keys.dk_kx_pub, offer_id=phone.offer, attempt=phone.attempt, dk_sig_pub=phone.sig_pub,
        dk_kx_pub=phone.kx_pub, label=phone.label, scopes_max=["look", "decide"], created_ms=T0,
    )  # fmt: skip
    sig = crypto.b64u(crypto.sign(WSK_PRIV, L["sig_cert_request"] + canon.dumps(o)))
    return {"o": o, "sig": sig}


@pytest.mark.parametrize("typed_right", [True, False])
def test_the_owner_signs_the_certificate_only_for_the_code_the_phone_shows(tmp_path, relay, typed_right):
    root, pid, pk = sandbox_with_card(tmp_path, relay)
    fields = pairing.PairFields(
        WS, os.urandom(16).hex(), os.urandom(32), crypto.wsk_pin(corpus.pub("wsk_a.sig")), crypto.pk_pin(pk)
    )
    phone = DevPhone(pairing.render_pair_link(pairing.PairLink(V_ORIGIN, True, fields)), now_ms=lambda: T0 + 1000)
    owner = owner_for(root, relay)
    request = cert_request_for(owner, phone, pid)
    fake = QueueRelay(relay, request)
    shown: list[tuple[str, str]] = []

    def read_code(label: str, scopes: str):
        shown.append((label, scopes))
        code = phone.check_challenge(fake.posted["challenge"]["challenge"], pk)  # the phone verifies, shows its code
        return code if typed_right else ("AAAAAA" if code != "AAAAAA" else "BBBBBB")

    out = owner.serve_request(
        request["o"]["request_id"], WS, root / "state", read_code, wait_s=5, poll_wait=1, sleep=lambda s: None
    )
    assert shown == [(phone.label, "look,decide")]
    if typed_right:
        o = certs.verify_cert(out.cert, pk, T0 + 1000)
        assert (o["device_id"], o["scopes_max"]) == (phone.device_id, ["look", "decide"])
        assert fake.devices == {phone.device_id: out.cert} and not out.rejected
    else:
        assert out.rejected and "rejection" in fake.posted and not fake.devices
