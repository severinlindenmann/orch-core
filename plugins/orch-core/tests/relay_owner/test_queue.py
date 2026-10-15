"""``orch.relay_owner.queue``: the primary's §8.4 steps against a simulated loopback relay, with requests built by the
host builder (``cert_request_o``) under the vector WSK and sealed to the vector primary's ``kx``; the journal under
one lock; hostile relays (forged, foreign, replayed, conflicting, oversized, expired requests); never re-signing."""

from __future__ import annotations

import json
import os

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import certreq, certs, pairing, wscard
from orch.relay_owner.journal import PrimaryJournal, journal_dir
from orch.relay_owner.queue import ANSWER_LABELS, PrimaryQueue, QueueRefused

from ..relay import corpus
from ..relay.mock import MockRelay, Reply, json_reply
from .kit import PERSON, PK, PRIMARY, VecPK, client

S2 = corpus.S2
CR = S2["cert_request"]
INNER = CR["inner"]
WS = CR["signed"]["o"]["workspace_id"]
CARD = wscard.verify_card(CR["cases"][0]["card"], PK)
T0 = CR["signed"]["o"]["created_ms"]
_KX = corpus.priv("primary.kx")


def unseal(object_id: bytes, sealed: bytes) -> bytes:
    return crypto.open_sealed(_KX, bytes.fromhex(PRIMARY), "cert-request", object_id, 0, sealed)


def make_request(rid: str, *, created=T0, scopes=("look", "decide"), primary=PRIMARY, wsk="wsk_a", ws=WS, **kw):
    o = certreq.cert_request_o(
        request_id=rid, workspace_id=ws, person_id=PERSON, primary_device_id=primary,
        primary_kx_pub=corpus.pub("primary.kx"), offer_id=INNER["offer_id"], attempt=INNER["attempt"],
        dk_sig_pub=crypto.unb64u(INNER["dk_sig_pub"]), dk_kx_pub=crypto.unb64u(INNER["dk_kx_pub"]),
        label=INNER["label"], scopes_max=list(scopes), created_ms=created,
    )  # fmt: skip
    o.update(kw)
    sig = crypto.sign(corpus.priv(f"{wsk}.sig"), L["sig_cert_request"] + canon.dumps(o))
    return {"o": o, "sig": crypto.b64u(sig)}


class FakeRelay:
    """The relay's side of the routes the primary uses, as a MockRelay fallback."""

    def __init__(self, relay: MockRelay) -> None:
        self.queue: list[dict] = []
        self.posted: list[tuple[str, dict]] = []
        self.devices: dict[str, dict] = {}
        self.fail: dict[str, Reply] = {}
        relay.fallback = self

    def __call__(self, s):
        if s.path in self.fail:
            return self.fail.pop(s.path)
        if s.path == "/v2/session/challenge":
            return json_reply(200, {"challenge": crypto.b64u(os.urandom(32)), "expires_ms": 1})
        if s.path == "/v2/session/login":
            return json_reply(200, {"token": "FAKE-primary", "expires_ms": 1})
        if s.path.startswith("/v2/persons/") and s.path.endswith("/cert-requests"):
            return json_reply(200, {"requests": self.queue})
        if s.path.startswith("/v2/cert-requests/"):
            self.posted.append((s.path, json.loads(s.body)))
            return json_reply(200, {})
        if s.method == "PUT" and s.path.startswith("/v2/devices/"):
            cert = json.loads(s.body)["cert"]
            self.devices[cert["o"]["device_id"]] = cert
            return json_reply(200, {"device_id": cert["o"]["device_id"]})
        return json_reply(404, {"error": "not_found"})


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


@pytest.fixture
def fake(relay):
    return FakeRelay(relay)


@pytest.fixture
def jdir(tmp_path):
    return tmp_path.resolve() / "requests"


def queue(relay, jdir, *, now=T0 + 1000, cards=None, pk_pub=PK, **kw):
    clock = [now]
    q = PrimaryQueue(
        client(relay), PrimaryJournal(jdir), pk_pub=pk_pub,
        cards=cards or (lambda ws: CARD if ws == WS else None), unseal=unseal, now_ms=lambda: clock[0], **kw,
    )  # fmt: skip
    q.clock = clock  # type: ignore[attr-defined]
    return q


RID = "1d" * 16


def test_the_full_primary_flow(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    poll = q.poll()
    p = poll.get(RID)
    assert poll.skipped == () and p.state == "verified" and p.within_policy
    assert p.label == pairing.clean_label(INNER["label"]) and p.scopes_max == ("look", "decide")
    pk = VecPK()
    p = q.challenge(p, pk)
    path, body = fake.posted[-1]
    assert path == f"/v2/cert-requests/{RID}/challenge"
    ch = certreq.check_cert_challenge(body["challenge"], PK, fake.queue[0]["o"])
    assert p.cert_code == ch.cert_code
    cert = q.sign(p, pk)
    o = certs.check_cert(cert, PK)
    assert fake.devices[o["device_id"]] == cert and o["device_id"] == INNER["device_id"]
    assert o["scopes_max"] == ["look", "decide"] and o["expires_ms"] is None
    assert pk.labels == ["sig_cert_challenge", "sig_device_cert"] and set(pk.labels) <= ANSWER_LABELS
    with q._journal.transaction() as v:
        assert v.get(RID).state == "uploaded"
    # the relay keeps listing it (or replays it): never acted on again
    again = q.poll()
    assert again.pending == () and [s.code for s in again.skipped] == ["closed"]


def test_reject(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    p = q.poll().get(RID)
    pk = VecPK()
    rej = q.reject(p, pk)
    assert fake.posted[-1] == (f"/v2/cert-requests/{RID}/rejection", {"rejection": rej})
    certreq.check_cert_rejection(rej, PK, fake.queue[0]["o"])
    with pytest.raises(QueueRefused) as e:
        q.sign(p, pk)
    assert e.value.code == "already_decided"
    assert q.reject(p, pk) == rej and pk.labels == ["sig_cert_rejection"]  # re-posted, never re-signed


def test_never_re_signs_a_challenge_and_resumes_from_the_journal(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    p = q.poll().get(RID)
    pk = VecPK()
    fake.fail[f"/v2/cert-requests/{RID}/challenge"] = Reply(503, body=b'{"error":"overloaded"}')
    with pytest.raises(Exception):  # noqa: B017 - the post failed after the journal write
        q.challenge(p, pk)
    fake.queue = []  # a challenged request is no longer listed: recoverable from the journal only
    q2 = queue(relay, jdir)
    p2 = q2.poll().get(RID)
    assert p2.state == "challenged" and p2.cert_code is not None
    p2 = q2.challenge(p2, pk)
    assert pk.labels == ["sig_cert_challenge"]  # once
    posted = [b for path, b in fake.posted if path.endswith("/challenge")]
    assert len(posted) == 1 and posted[0]["challenge"] == p2.challenge


def test_a_decided_upload_is_finished_from_the_stored_bytes(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    pk = VecPK()
    p = q.challenge(q.poll().get(RID), pk)
    fake.fail[f"/v2/devices/{INNER['device_id']}"] = Reply(500, body=b"{}")
    with pytest.raises(Exception):  # noqa: B017
        q.sign(p, pk)
    assert fake.devices == {}
    assert queue(relay, jdir).finish_uploads() == [RID]
    assert pk.labels == ["sig_cert_challenge", "sig_device_cert"] and INNER["device_id"] in fake.devices


def test_sign_needs_a_challenge(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    pk = VecPK()
    with pytest.raises(QueueRefused) as e:
        q.sign(q.poll().get(RID), pk)
    assert e.value.code == "not_challenged" and pk.labels == []


def test_nothing_is_signed_after_expiry(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    p = q.poll().get(RID)
    q.clock[0] = p.expires_ms
    pk = VecPK()
    for act in (q.challenge, q.reject):
        with pytest.raises(QueueRefused) as e:
            act(p, pk)
        assert e.value.code in ("expired", "not_live")
    assert pk.labels == [] and fake.posted == []


def test_another_persons_key_signs_nothing(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    bob = VecPK("person_bob")
    with pytest.raises(QueueRefused) as e:
        q.challenge(q.poll().get(RID), bob)
    assert e.value.code == "person_mismatch" and bob.labels == []


def test_scopes_above_the_v17_phone_ceiling_can_be_rejected_but_never_challenged(relay, fake, jdir):
    fake.queue = [make_request(RID, scopes=("look", "decide", "operate"))]
    q = queue(relay, jdir)
    p = q.poll().get(RID)
    assert not p.within_policy
    pk = VecPK()
    with pytest.raises(QueueRefused) as e:
        q.challenge(p, pk)
    assert e.value.code == "scope_exceeded" and pk.labels == []
    q.reject(p, pk)
    assert pk.labels == ["sig_cert_rejection"]


def test_a_wider_policy_is_the_callers_explicit_choice(relay, fake, jdir):
    fake.queue = [make_request(RID, scopes=("look", "decide", "operate", "type"))]
    q = queue(relay, jdir, max_scopes=("look", "decide", "operate", "type"))
    assert q.poll().get(RID).within_policy


# --- hostile relays -------------------------------------------------------------------------------------------------


def codes(poll):
    return sorted((s.request_id, s.code) for s in poll.skipped)


def burned(jdir, rid):
    with PrimaryJournal(jdir).transaction() as v:
        e = v.get(rid)
        return e is not None and e.state == "refused"


@pytest.mark.parametrize(
    "req,code,burns",
    [
        (lambda: make_request(RID, wsk="wsk_b"), "bad_signature", False),  # forged: another WSK
        (lambda: make_request(RID, primary="ab" * 16), "not_for_this_device", False),
        (lambda: make_request(RID, created=T0 - 3_600_000), "expired", True),
        (lambda: {**make_request(RID), "sig": crypto.b64u(b"\x01" * 64)}, "bad_signature", False),
        (lambda: make_request(RID, sealed=crypto.b64u(b"\x04" * 120)), "malformed", True),  # does not open
    ],
)
def test_hostile_requests_are_skipped_and_burned_only_once_authenticated(relay, fake, jdir, req, code, burns):
    fake.queue = [req()]
    q = queue(relay, jdir)
    poll = q.poll()
    assert poll.pending == () and codes(poll) == [(RID, code)]
    assert burned(jdir, RID) == burns


def test_a_request_of_an_unknown_workspace_is_left_alone(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir, cards=lambda ws: None)
    assert codes(q.poll()) == [(RID, "unknown_workspace")] and not burned(jdir, RID)


def test_a_card_of_another_person_is_refused(relay, fake, jdir):
    """A primary of bob holding alice's card: ``other_person`` before any signature check, nothing burned."""
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir, pk_pub=corpus.pub("person_bob.sig"))
    assert codes(q.poll()) == [(RID, "other_person")] and not burned(jdir, RID)


def test_a_replayed_refused_id_stays_refused(relay, fake, jdir):
    fake.queue = [make_request(RID, created=T0 - 3_600_000)]
    q = queue(relay, jdir)
    q.poll()
    fake.queue = [make_request(RID)]  # the same id, now inside its window: never judged fresh again
    assert codes(q.poll()) == [(RID, "closed")] and burned(jdir, RID)


def test_the_same_id_with_other_bytes_is_a_conflict_and_the_stored_one_wins(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll()
    fake.queue = [make_request(RID, created=T0 + 1)]
    poll = q.poll()
    assert (RID, "primary_request_conflict") in codes(poll)
    assert poll.get(RID).request.created_ms == T0  # the journaled request, resumed


def test_a_second_open_request_for_the_workspace_is_refused_and_burned(relay, fake, jdir):
    other = "2e" * 16
    fake.queue = [make_request(RID), make_request(other)]
    poll = queue(relay, jdir).poll()
    assert [p.request_id for p in poll.pending] == [RID]
    assert codes(poll) == [(other, "request_open")] and burned(jdir, other)


def test_an_oversized_listed_request_is_never_checked_or_journaled(relay, fake, jdir):
    big = make_request(RID)
    big["o"] = {**big["o"], "sealed": "A" * 9000}
    fake.queue = [big]
    assert codes(queue(relay, jdir).poll()) == [(RID, "too_large")] and not burned(jdir, RID)


def test_a_listed_entry_without_a_valid_id_is_skipped(relay, fake, jdir):
    fake.queue = [{"o": {"request_id": "../x"}, "sig": ""}]
    assert codes(queue(relay, jdir).poll()) == [(None, "malformed")]


def test_a_pending_of_another_journal_is_refused(relay, fake, jdir, tmp_path):
    fake.queue = [make_request(RID)]
    p = queue(relay, jdir).poll().get(RID)
    other = queue(relay, tmp_path.resolve() / "other")
    with pytest.raises(QueueRefused) as e:
        other.challenge(p, VecPK())
    assert e.value.code == "not_this_request"


def test_close_ends_a_live_request(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    p = q.poll().get(RID)
    q.close(RID, "cancelled")
    with pytest.raises(QueueRefused):
        q.challenge(p, VecPK())
    with pytest.raises(QueueRefused):
        q.close(RID, "cancelled")


def test_a_corrupt_journal_record_never_blocks_the_next_pairing(relay, fake, jdir):
    """orch-52's lockout question end to end: one damaged record burns its id only."""
    other = "2e" * 16
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.close(q.poll().get(RID).request_id, "cancelled")
    (jdir / f"{RID}.json").write_bytes(b"garbage")
    fake.queue = [make_request(other)]
    poll = q.poll()
    assert [p.request_id for p in poll.pending] == [other]
    with PrimaryJournal(jdir).transaction() as v:
        assert v.get(RID).tombstone and v.corrupt == set()


def test_a_full_journal_skips_new_requests_without_failing(relay, fake, jdir, monkeypatch):
    from orch.relay_owner import journal as jmod

    monkeypatch.setattr(jmod, "MAX_ENTRIES", 0)
    fake.queue = [make_request(RID)]
    assert codes(queue(relay, jdir).poll()) == [(RID, "journal.full")]


def test_the_custody_primary_session_is_the_signer_and_the_unseal(relay, fake, tmp_path, monkeypatch):
    """The product wiring: ``orch.custody.primary.unlocked`` yields the login signer, ``cert_request_unseal`` the
    opener; one passphrase prompt for the whole command."""
    from orch.custody import KdfParams, PassphraseBackend, primary
    from orch.relay import http as rhttp
    from orch.relay_owner import OwnerClient

    from ..relay.helpers import V_ORIGIN
    from .kit import OPEN

    prompts = []
    monkeypatch.setattr(primary, "open_backend", lambda d: PassphraseBackend(
        d, _passphrase_provider=lambda r: prompts.append(r.kind) or "Zq7!mPx2-vL9#rTb4w",
        _kdf=KdfParams(n=2**10), _min_n=2**10))  # fmt: skip
    state = tmp_path.resolve() / "state"
    info = primary.init(state)
    o = certreq.cert_request_o(
        request_id=RID, workspace_id=WS, person_id=PERSON, primary_device_id=info.device_id.hex(),
        primary_kx_pub=info.dk_kx_pub, offer_id=INNER["offer_id"], attempt=INNER["attempt"],
        dk_sig_pub=crypto.unb64u(INNER["dk_sig_pub"]), dk_kx_pub=crypto.unb64u(INNER["dk_kx_pub"]),
        label=INNER["label"], scopes_max=["look", "decide"], created_ms=T0,
    )  # fmt: skip
    sig = crypto.sign(corpus.priv("wsk_a.sig"), L["sig_cert_request"] + canon.dumps(o))
    fake.queue = [{"o": o, "sig": crypto.b64u(sig)}]
    with primary.unlocked(state, action="pair a device") as session:
        conn = rhttp.RelayConn(relay.origin())
        OPEN.append(conn)
        c = OwnerClient(V_ORIGIN, session, transport=conn)
        q = PrimaryQueue(
            c, PrimaryJournal(journal_dir(state)), pk_pub=PK, cards=lambda ws: CARD,
            unseal=primary.cert_request_unseal(session), now_ms=lambda: T0 + 1,
        )  # fmt: skip
        p = q.poll().get(RID)
        assert p is not None and p.inner.device_id == INNER["device_id"]
    assert prompts == ["create", "primary"]
