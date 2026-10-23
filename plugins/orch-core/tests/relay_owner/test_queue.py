"""``orch.relay_owner.queue``: the primary's §8.4 steps against a simulated loopback relay, with requests built by the
host builder (``cert_request_o``) under the vector WSK and sealed to the vector primary's ``kx``; the journal under
one lock; hostile relays (forged, foreign, replayed, conflicting, oversized, expired requests); never re-signing."""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import certreq, certs, pairing, wscard
from orch.relay_owner import OwnerRefused
from orch.relay_owner.journal import RETENTION_MS, PrimaryJournal, journal_dir
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
            return Reply(200, body=json.dumps({"requests": self.queue}).encode())  # not cj: may be hostile
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
    poll = q.poll(RID)
    p = poll.get(RID)
    assert poll.skipped == () and p.state == "verified" and p.within_policy
    assert p.label == pairing.clean_label(INNER["label"]) and p.scopes_max == ("look", "decide")
    pk = VecPK()
    p = q.challenge(RID, pk)
    path, body = fake.posted[-1]
    assert path == f"/v2/cert-requests/{RID}/challenge"
    ch = certreq.check_cert_challenge(body["challenge"], PK, fake.queue[0]["o"])
    assert p.cert_code == ch.cert_code
    cert = q.sign(RID, pk)
    o = certs.check_cert(cert, PK)
    assert fake.devices[o["device_id"]] == cert and o["device_id"] == INNER["device_id"]
    assert o["scopes_max"] == ["look", "decide"] and o["expires_ms"] is None
    assert pk.labels == ["sig_cert_challenge", "sig_device_cert"] and set(pk.labels) <= ANSWER_LABELS
    with q._journal.transaction() as v:
        assert v.get(RID).state == "uploaded"
    again = q.poll(RID)  # the relay keeps listing it (or replays it): never acted on again
    assert again.pending == () and codes(again) == [(RID, "closed")]


def test_reject(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    pk = VecPK()
    rej = q.reject(RID, pk)
    assert fake.posted[-1] == (f"/v2/cert-requests/{RID}/rejection", {"rejection": rej})
    certreq.check_cert_rejection(rej, PK, fake.queue[0]["o"])
    with pytest.raises(QueueRefused) as e:
        q.sign(RID, pk)
    assert e.value.code == "already_decided"
    assert q.reject(RID, pk) == rej and pk.labels == ["sig_cert_rejection"]  # re-posted, never re-signed


def test_never_re_signs_a_challenge_and_resumes_from_the_journal(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    pk = VecPK()
    fake.fail[f"/v2/cert-requests/{RID}/challenge"] = Reply(503, body=b'{"error":"overloaded"}')
    with pytest.raises(OwnerRefused):  # the post failed after the journal write
        q.challenge(RID, pk)
    fake.queue = []  # a challenged request is no longer listed: recoverable from the journal only
    q2 = queue(relay, jdir)
    p2 = q2.poll(RID).get(RID)
    assert p2.state == "challenged" and p2.cert_code is not None
    assert q2.challenge(RID, pk).cert_code == p2.cert_code
    assert pk.labels == ["sig_cert_challenge"]  # once
    posted = [b for path, b in fake.posted if path.endswith("/challenge")]
    assert len(posted) == 1 and pairing.cert_code(posted[0]["challenge"]["o"]) == p2.cert_code


def test_a_decided_upload_is_finished_from_the_stored_bytes(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    pk = VecPK()
    q.poll(RID)
    q.challenge(RID, pk)
    fake.fail[f"/v2/devices/{INNER['device_id']}"] = Reply(500, body=b"{}")
    with pytest.raises(OwnerRefused):
        q.sign(RID, pk)
    assert fake.devices == {}
    assert queue(relay, jdir).finish_uploads() == {RID: "uploaded"}
    assert pk.labels == ["sig_cert_challenge", "sig_device_cert"] and INNER["device_id"] in fake.devices


def test_a_permanent_upload_refusal_is_durable_and_never_stops_the_others(relay, fake, jdir):
    """Review of #557 (Codex P2): ``cert_conflict`` ends one decision ``upload_failed``; the next decision still
    uploads; ``close`` ends a decision stuck on transient failures."""
    other, third = "2e" * 16, "3f" * 16
    pk = VecPK()
    for rid in (RID, other, third):  # a decided request no longer blocks its workspace
        fake.queue = [make_request(rid)]
        q = queue(relay, jdir)
        q.poll(rid)
        fake.fail[f"/v2/cert-requests/{rid}/rejection"] = Reply(503, body=b'{"error":"overloaded"}')
        with pytest.raises(OwnerRefused):
            q.reject(rid, pk)
    fake.fail[f"/v2/cert-requests/{RID}/rejection"] = json_reply(410, {"error": "expired"})
    fake.fail[f"/v2/cert-requests/{third}/rejection"] = Reply(503, body=b'{"error":"overloaded"}')
    assert q.finish_uploads() == {RID: "upload_failed", other: "uploaded", third: "retry"}
    assert q.finish_uploads() == {third: "uploaded"}
    with q._journal.transaction() as v:
        assert [v.get(r).state for r in (RID, other, third)] == ["upload_failed", "uploaded", "uploaded"]
        assert RID in v.cert_journal().closed


def test_close_ends_a_decision_that_never_uploads(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    fake.fail[f"/v2/cert-requests/{RID}/rejection"] = Reply(503, body=b'{"error":"overloaded"}')
    with pytest.raises(OwnerRefused):
        q.reject(RID, VecPK())
    q.close(RID, "cancelled")
    assert q.finish_uploads() == {}
    with q._journal.transaction() as v:
        assert v.get(RID).state == "upload_failed"


def test_sign_needs_a_challenge(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    pk = VecPK()
    with pytest.raises(QueueRefused) as e:
        q.sign(RID, pk)
    assert e.value.code == "not_challenged" and pk.labels == []


def test_nothing_is_signed_after_expiry(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    p = q.poll(RID).get(RID)
    q.clock[0] = p.expires_ms
    pk = VecPK()
    for act in (q.challenge, q.reject):
        with pytest.raises(QueueRefused) as e:
            act(RID, pk)
        assert e.value.code in ("expired", "not_live")
    assert pk.labels == [] and fake.posted == []


def test_another_persons_key_signs_nothing(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    bob = VecPK("person_bob")
    with pytest.raises(QueueRefused) as e:
        q.challenge(RID, bob)
    assert e.value.code == "person_mismatch" and bob.labels == []


def test_scopes_above_the_v17_phone_ceiling_can_be_rejected_but_never_challenged(relay, fake, jdir):
    fake.queue = [make_request(RID, scopes=("look", "decide", "operate"))]
    q = queue(relay, jdir)
    assert not q.poll(RID).get(RID).within_policy
    pk = VecPK()
    with pytest.raises(QueueRefused) as e:
        q.challenge(RID, pk)
    assert e.value.code == "scope_exceeded" and pk.labels == []
    q.reject(RID, pk)
    assert pk.labels == ["sig_cert_rejection"]


def test_a_wider_policy_is_the_callers_explicit_choice(relay, fake, jdir):
    fake.queue = [make_request(RID, scopes=("look", "decide", "operate", "type"))]
    q = queue(relay, jdir, max_scopes=("look", "decide", "operate", "type"))
    assert q.poll(RID).get(RID).within_policy


# --- review of #557, item 1: the actions take the request id only ----------------------------------------------------


def test_a_doctored_snapshot_cannot_widen_the_policy(relay, fake, jdir):
    """Opus probe 1: ``replace(p, within_policy=True)`` got an over-scoped request signed. The actions now take the
    id only and recompute the policy from the stored request."""
    fake.queue = [make_request(RID, scopes=("look", "decide", "operate"))]
    q = queue(relay, jdir)
    p = dataclasses.replace(q.poll(RID).get(RID), within_policy=True, scopes_max=("look",))
    pk = VecPK()
    for act in (q.challenge, q.sign):
        with pytest.raises((QueueRefused, TypeError)):
            act(p, pk)  # a snapshot is not an id
        with pytest.raises(QueueRefused) as e:
            act(p.request_id, pk)
        assert e.value.code in ("scope_exceeded", "not_challenged")
    assert pk.labels == []


def test_sign_uses_the_stored_request_and_challenge_never_another_inner(relay, fake, jdir):
    """Opus probe 2: sign() used another request's inner. Now the inner is re-opened from the stored request, and it
    must name the stored challenge's device and keys: a challenge journaled for other keys stops the certificate."""
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    pk = VecPK()
    q.challenge(RID, pk)
    with q._journal.transaction() as v:  # simulate a stored challenge for another device (a damaged or forged record)
        e = v.get(RID)
        o = {**e.challenge["o"], "device_id": "ab" * 16}
        forged = canon.dumps({"o": o, "sig": e.challenge["sig"]})
        v._write(dataclasses.replace(e, challenge_cj=forged))
    with pytest.raises(QueueRefused) as err:
        q.sign(RID, pk)
    assert err.value.code == "not_this_request" and pk.labels == ["sig_cert_challenge"] and fake.devices == {}


def test_an_unknown_or_bad_id_is_refused(relay, fake, jdir):
    q = queue(relay, jdir)
    for rid in ("2e" * 16, "../x", None):
        with pytest.raises(QueueRefused):
            q.challenge(rid, VecPK())


# --- hostile relays -------------------------------------------------------------------------------------------------


def codes(poll):
    return sorted((s.request_id or "", s.code) for s in poll.skipped)


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
    poll = queue(relay, jdir).poll(RID)
    assert poll.pending == () and codes(poll) == [(RID, code)]
    assert burned(jdir, RID) == burns


def test_a_request_of_an_unknown_workspace_is_left_alone(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir, cards=lambda ws: None)
    assert codes(q.poll(RID)) == [(RID, "unknown_workspace")] and not burned(jdir, RID)


def test_a_card_of_another_person_is_refused(relay, fake, jdir):
    """A primary of bob holding alice's card: ``other_person`` before any signature check, nothing burned."""
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir, pk_pub=corpus.pub("person_bob.sig"))
    assert codes(q.poll(RID)) == [(RID, "other_person")] and not burned(jdir, RID)


def test_a_replayed_refused_id_stays_refused(relay, fake, jdir):
    fake.queue = [make_request(RID, created=T0 - 3_600_000)]
    q = queue(relay, jdir)
    q.poll(RID)
    fake.queue = [make_request(RID)]  # the same id, now inside its window: never judged fresh again
    assert codes(q.poll(RID)) == [(RID, "primary_request_conflict")] and burned(jdir, RID)


def test_the_same_id_with_other_bytes_is_a_conflict_and_the_stored_one_wins(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    fake.queue = [make_request(RID, created=T0 + 1)]
    poll = q.poll(RID)
    assert codes(poll) == [(RID, "primary_request_conflict")]
    assert poll.get(RID) is not None  # the journaled request, resumed
    with q._journal.transaction() as v:
        assert v.get(RID).request["o"]["created_ms"] == T0


def test_only_the_reported_request_is_worked_others_are_left_alone(relay, fake, jdir):
    """Review of #557, item 4 (DECIDED): other listed requests are skipped ``other_open``, neither opened nor
    journaled."""
    other = "2e" * 16
    fake.queue = [make_request(other), make_request(RID)]
    poll = queue(relay, jdir).poll(RID)
    assert [p.request_id for p in poll.pending] == [RID] and codes(poll) == [(other, "other_open")]
    with PrimaryJournal(jdir).transaction() as v:
        assert v.get(other) is None


def test_a_second_open_request_for_the_workspace_is_refused_and_burned(relay, fake, jdir):
    other = "2e" * 16
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    fake.queue = [make_request(RID), make_request(other)]
    poll = q.poll(other)
    assert poll.pending == () and codes(poll) == [(RID, "other_open"), (other, "request_open")]
    assert burned(jdir, other)


def test_an_oversized_listed_request_is_never_checked_or_journaled(relay, fake, jdir):
    big = make_request(RID)
    big["o"] = {**big["o"], "sealed": "A" * 9000}
    fake.queue = [big]
    assert codes(queue(relay, jdir).poll(RID)) == [(RID, "too_large")] and not burned(jdir, RID)


def test_a_listed_entry_without_a_valid_id_is_skipped(relay, fake, jdir):
    fake.queue = [{"o": {"request_id": "../x"}, "sig": ""}]
    assert codes(queue(relay, jdir).poll(RID)) == [("", "malformed")]


def deep(n):
    x: object = 1
    for _ in range(n):
        x = [x]
    return x


def test_a_deeply_nested_listed_entry_is_malformed_not_a_crash(relay, fake, jdir):
    """Review of #557, item 3: nesting deeper than the protocol's 16 (allowed by the HTTP body's 32) is skipped as
    ``malformed``; the other entries are still worked."""
    fake.queue = [{"o": {"request_id": "2e" * 16, "x": deep(20)}, "sig": ""}, make_request(RID)]
    poll = queue(relay, jdir).poll(RID)
    assert codes(poll) == [("2e" * 16, "malformed")] and poll.get(RID) is not None


def test_a_deeply_nested_copy_of_a_journaled_id_is_malformed_not_a_crash(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    fake.queue = [{"o": {**make_request(RID)["o"], "x": deep(20)}, "sig": ""}]
    poll = q.poll(RID)
    assert codes(poll) == [(RID, "malformed")] and poll.get(RID) is not None  # resumed from the journal


def test_a_re_listed_tombstone_is_closed_not_a_conflict(relay, fake, jdir):
    """Opus delta D1 on #557: an id older than retention (a tombstone, no bytes kept) re-listed by the relay is
    reported ``closed``, not a false ``primary_request_conflict``."""
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    q.reject(RID, VecPK())
    late = queue(relay, jdir, now=T0 + RETENTION_MS + 700_000)
    poll = late.poll(RID)
    assert poll.pending == () and codes(poll) == [(RID, "closed")]
    with late._journal.transaction() as v:
        assert v.get(RID).tombstone


def test_close_works_on_a_live_request_whose_card_is_gone(relay, fake, jdir):
    """Review of #557, item 6."""
    fake.queue = [make_request(RID)]
    queue(relay, jdir).poll(RID)
    gone = queue(relay, jdir, cards=lambda ws: None)
    assert codes(gone.poll(RID)) == [(RID, "unknown_workspace")]
    gone.close(RID, "cancelled")
    with gone._journal.transaction() as v:
        assert v.get(RID).state == "cancelled"


def test_a_resume_after_retention_finds_an_expired_tombstone_never_a_live_one(relay, fake, jdir):
    """Review of #557, item 6: a live record that ages past retention is ended expired before it is tombstoned."""
    fake.queue = [make_request(RID)]
    queue(relay, jdir).poll(RID)
    fake.queue = []
    late = queue(relay, jdir, now=T0 + RETENTION_MS + 700_000)
    assert late.poll(RID).pending == ()
    with late._journal.transaction() as v:
        t = v.get(RID)
        assert t.tombstone and t.state == "expired" and v.cert_journal().open == ()
    with pytest.raises(QueueRefused):
        late.close(RID, "cancelled")


def test_close_ends_a_live_request(relay, fake, jdir):
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    q.close(RID, "cancelled")
    with pytest.raises(QueueRefused):
        q.challenge(RID, VecPK())
    with pytest.raises(QueueRefused):
        q.close(RID, "cancelled")


def test_a_corrupt_journal_record_never_blocks_the_next_pairing(relay, fake, jdir):
    """orch-52's lockout question end to end: one damaged record burns its id only."""
    other = "2e" * 16
    fake.queue = [make_request(RID)]
    q = queue(relay, jdir)
    q.poll(RID)
    q.close(RID, "cancelled")
    (jdir / f"{RID}.json").write_bytes(b"garbage")
    fake.queue = [make_request(other)]
    poll = q.poll(other)
    assert [p.request_id for p in poll.pending] == [other]
    with PrimaryJournal(jdir).transaction() as v:
        assert v.get(RID).tombstone and v.corrupt == set()


def test_a_full_journal_skips_new_requests_without_failing(relay, fake, jdir, monkeypatch):
    from orch.relay_owner import journal as jmod

    monkeypatch.setattr(jmod, "MAX_ENTRIES", 0)
    fake.queue = [make_request(RID)]
    assert codes(queue(relay, jdir).poll(RID)) == [(RID, "journal.full")]


def test_the_custody_primary_session_is_the_signer_and_the_unseal(relay, fake, tmp_path, monkeypatch):
    """The product wiring: ``orch.custody.primary.unlocked`` yields the login signer, ``cert_request_unseal`` the
    opener; one passphrase prompt for the whole command."""
    from orch.custody import KdfParams, PassphraseBackend, primary
    from orch.net import http as rhttp
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
        p = q.poll(RID).get(RID)
        assert p is not None and p.device_id == INNER["device_id"]
    assert prompts == ["create", "primary"]
