"""H12e: the core side of ``relay.submit`` admission (orch-v2-host §11A.14 "As built in H12e").

``judge_events`` is the model's judgement of submitted, device-signed events shared by ``human.submit`` and the relay
(one shared test below); the L-span helpers of :mod:`orch.host.admission` are what R2 calls inside one
``store.locked()`` span: the dedup by signed content (L3), the target checks (L4, L5: the exact next key), and the
P6 freshness window. Everything runs on a real store with real signatures."""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
from typing import Any

import pytest

from orch import canon, crypto
from orch.host import admission
from orch.host.errors import HostError
from orch.host.human import HumanOps, judge_events
from orch.identity import certs, new_ulid
from tests.identity.helpers import Person
from tests.store.helpers import WS, Env


class Phone:
    """A second device of a person: a P-256 key whose certificate allows look and decide only (the phone of P4)."""

    def __init__(self, env: Env, p: Person) -> None:
        self.p = p
        self.key = crypto.generate_private_key()
        pub = crypto.public_bytes(self.key)
        cert = certs.make_device_cert(
            p.pk_pub,
            p.pk_sign,
            dk_sig_pub=pub,
            dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
            label_sealed=b"phone",
            created_ms=env.clock[0] * 1000,
            expires_ms=None,
            scopes_max=["look", "decide"],
        )
        self.device = certs.device_ref(pub)
        assert env.store is not None
        env.store.append(
            env.person_event(p, "workspace", "device.added", device=self.device, cert=cert), log="workspace"
        )

    def event(self, env: Env, log: str, typ: str, *, based_on: Any = "head", auth: str = "secure-enclave-unlocked",
              eid: str | None = None, **payload: Any) -> dict[str, Any]:  # fmt: skip
        """The submitted form of 14.1: no seq, at, prev, ws_seq or host_sig; signed over 14.2's bytes."""
        s = env.store
        assert s is not None
        e: dict[str, Any] = {
            "v": 2,
            "id": eid or new_ulid(int(env.clock[0] * 1000)),
            "type": typ,
            "actor": {"kind": "person", "id": self.p.ref, "device": self.device},
            "auth": auth,
            "hash_v": 1,
            "roster_v": s.state.workspace.roster_v,
            "based_on": s.log_head(log) if based_on == "head" else based_on,
            **payload,
        }
        e["sig"] = crypto.b64u(crypto.sign(self.key, canon.person_signing_bytes(WS, log, e)))
        return e


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.bootstrap()
    try:
        yield e
    finally:
        assert e.store is not None
        e.store.close()


def ask(env: Env, uid: str, qn: str = "Q1", text: str = "Which tariff export?", options: Any = None) -> str:
    """An agent's ``question.asked`` (to the ticket owner); returns the question hash."""
    assert env.store is not None
    q: dict[str, Any] = {"id": qn, "to": "ticket_owner", "text": text, "blocking": True}
    q["kind"] = "choice" if options is not None else "text"
    if options is not None:
        q["options"] = options
    qid = canon.question_id(WS, uid, qn)
    h = canon.question_hash(qid, uid, q)
    env.store.append({"type": "question.asked", "actor": env.agent, "question": q, "qid": qid, "hash": h}, log=uid)
    return h


OPTS = [{"key": "csv", "label": "Monthly CSV", "cost": "1 day"}, {"key": "api", "label": "Tariff API"}]


@pytest.fixture
def phone(env):
    return Phone(env, env.owner)


# --- judge_events: one judgement for human.submit and relay.submit --------------------------------------------------


def test_judge_events_accepts_a_phone_answer_and_appends_nothing(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    store = env.store
    before = store.log_head(uid)
    with store.locked():
        assert judge_events(store, uid, [ev]) is None
    assert store.log_head(uid) == before  # a judgement only: nothing lands
    store.append(ev, log=uid)  # and the judged event is exactly what the store accepts
    assert store.logged_event(uid, ev["id"])["sig"] == ev["sig"]


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda e, h: {**e, "hash": "sha256:" + "0" * 64}, "question.stale"),
        (lambda e, h: {**e, "option": "pdf"}, "answer.bad_option"),
        (lambda e, h: {**e, "roster_v": e["roster_v"] - 1}, "members.stale"),
        (lambda e, h: {**e, "based_on": "sha256:" + "1" * 64}, "event.bad_base"),
        (lambda e, h: {**e, "question": "Q9"}, "question.unknown"),
    ],
)
def test_judge_events_refuses_with_the_models_code(env, phone, change, code):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    bad = change({k: v for k, v in ev.items() if k != "sig"}, h)
    bad["sig"] = crypto.b64u(crypto.sign(phone.key, canon.person_signing_bytes(WS, uid, bad)))
    with pytest.raises(HostError) as exc, env.store.locked():
        judge_events(env.store, uid, [bad])
    assert exc.value.code == code


def test_judge_events_verifies_the_signature(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    ev["option"] = "api"  # changed after signing
    with pytest.raises(HostError) as exc:
        judge_events(env.store, uid, [ev])
    assert exc.value.code == "sig.invalid"


def test_judge_events_refuses_an_operate_event_from_the_phone_certificate(env, phone):
    ev = phone.event(env, "workspace", "settings.changed", set={"grant_hours": 4})
    with pytest.raises(HostError) as exc:
        judge_events(env.store, "workspace", [ev])
    assert exc.value.code == "device.scope"


def test_judge_events_judges_a_creation_with_based_on_null(env, phone):
    uid = new_ulid()
    key = env.store.peek_key()
    ev = phone.event(
        env, uid, "ticket.created", based_on=None, key=key, ticket_type="feature", title="Check the tariff seeds",
        owner=env.owner.ref,
    )  # fmt: skip
    judge_events(env.store, uid, [ev])
    log2 = new_ulid()
    taken = phone.event(
        env, log2, "ticket.created", based_on=None, key="DEMO-0001", ticket_type="bug", title="x",
        owner=env.owner.ref,
    )  # fmt: skip
    env.new_ticket()  # DEMO-0001 exists now
    with pytest.raises(HostError) as exc:
        judge_events(env.store, log2, [taken])
    assert exc.value.code == "ticket.exists"


def test_judge_events_and_human_submit_share_one_judgement(env, monkeypatch):
    """``HumanOps._judge`` is a thin wrapper: the prepared log and copies, then :func:`judge_events`."""
    seen: list[tuple[Any, ...]] = []

    def spy(store, log, events, *, git=None, copies=None):
        seen.append((store, log, events, git, copies))

    monkeypatch.setattr("orch.host.human.judge_events", spy)
    record = {"prepared": {"log": "01J9ZP0000000000000000000T"}, "copies": [{"repo": "r"}]}
    HumanOps._judge("S", record, ["E"], "G")
    assert seen == [("S", "01J9ZP0000000000000000000T", ["E"], "G", [{"repo": "r"}])]


def test_judge_events_without_git_fails_closed_on_a_source_list(env, phone):
    uid = env.new_ticket()
    ev = phone.event(env, uid, "log.added", text="x")
    ev["source_sha"] = []
    with pytest.raises(HostError) as exc:
        judge_events(env.store, uid, [ev])
    assert exc.value.code == "internal"


# --- L3: dedup by signed content and actor, never by signature bytes ------------------------------------------------


def high_s(sig: str) -> str:
    raw = _b64u_decode(sig)
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    r, s = raw[:32], int.from_bytes(raw[32:], "big")
    return crypto.b64u(r + (n - s).to_bytes(32, "big"))


def _b64u_decode(s: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_signed_digest_is_sha256_of_the_person_signing_bytes(env, phone):
    uid = env.new_ticket()
    ev = phone.event(env, uid, "log.added", text="x")
    want = "sha256:" + hashlib.sha256(canon.person_signing_bytes(WS, uid, ev)).hexdigest()
    assert admission.signed_digest(WS, uid, ev) == want
    # sig and the host stamp are not part of the content
    stamped = {**ev, "sig": high_s(ev["sig"]), "seq": 9, "at": "2030-01-01T00:00:00Z", "prev": None, "ws_seq": 1}
    assert admission.signed_digest(WS, uid, stamped) == want


def test_dedup_of_a_new_id_is_none(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    with env.store.locked():
        assert admission.lookup(env.store, uid, ev["id"]) is None
        assert admission.dedup(env.store, uid, ev) is None


def test_dedup_of_the_same_content_is_the_landed_seq_also_with_other_signature_bytes(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    seq = env.store.append(ev, log=uid).event["seq"]
    found = admission.lookup(env.store, uid, ev["id"])
    assert found == admission.Logged(seq=seq, digest=admission.signed_digest(WS, uid, ev), actor=ev["actor"])
    assert admission.dedup(env.store, uid, ev) == seq
    assert admission.dedup(env.store, uid, {**ev, "sig": high_s(ev["sig"])}) == seq  # randomized / high-s: same


@pytest.mark.parametrize(
    "change",
    [
        lambda e: {**e, "option": "api"},
        lambda e: {**e, "auth": "passphrase"},
        lambda e: {**e, "actor": {**e["actor"], "device": "d_" + "0" * 32}},
    ],
)
def test_dedup_of_the_same_id_with_other_content_or_actor_is_event_duplicate_id(env, phone, change):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    env.store.append(ev, log=uid)
    with pytest.raises(HostError) as exc:
        admission.dedup(env.store, uid, change(ev))
    assert exc.value.code == "event.duplicate_id"


def test_dedup_of_an_id_a_restore_abandoned_is_event_abandoned(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    store = env.store
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    gone = store.append(ev, log=uid).event
    store.checkpoint()
    store.abandon_tail(uid, gone["seq"] - 1)  # the owner rolls the ticket back to the question
    facts = store.restore_facts(uid)
    assert facts["abandoned_decisions"] == [ev["id"]]
    store.append(env.person_event(env.owner, uid, "restore", **facts, reason="rollback"), log=uid)
    assert store.logged_event(uid, ev["id"]) is None
    assert admission.lookup(env.store, uid, ev["id"]) == "abandoned"
    with pytest.raises(HostError) as exc:
        admission.dedup(store, uid, ev)
    assert exc.value.code == "event.abandoned"


# --- L4, L5: the target ---------------------------------------------------------------------------------------------


def test_check_target_of_an_answer_needs_a_ticket_the_person_may_see(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    admission.check_target(env.store, uid, ev)
    missing = new_ulid()
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, missing, ev)
    assert exc.value.code == "not_found"
    other = Person()
    env.store.append(
        env.person_event(env.owner, "workspace", "member.added", person=other.ref, name="Mara", role="member",
                         pk_pub=crypto.b64u(other.pk_pub), device_cert=other.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    env.store.append(
        env.person_event(env.owner, uid, "visibility.changed", visibility={"restricted": [other.ref]}), log=uid
    )
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, uid, ev)  # restricted to another person: uniform with missing
    assert exc.value.code == "not_found" and exc.value.message == "no such ticket"


def creation(env: Env, phone: Phone, uid: str, **over: Any) -> dict[str, Any]:
    payload = {"key": env.store.peek_key(), "ticket_type": "feature", "title": "Check the tariff seeds",
               "owner": env.owner.ref, **over}  # fmt: skip
    return phone.event(env, uid, "ticket.created", based_on=None, **payload)


def test_check_target_of_a_creation_takes_exactly_the_next_key(env, phone):
    uid = new_ulid()
    ev = creation(env, phone, uid)
    with env.store.locked():
        admission.check_target(env.store, uid, ev)
    env.new_ticket()  # someone else takes that key first
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, uid, ev)
    assert exc.value.code == "ticket.key_not_next"
    assert exc.value.detail == {"next_key": env.store.peek_key()}
    assert (exc.value.exit, exc.value.retryable) == (3, False)
    ahead = creation(env, phone, uid, key="DEMO-0099")  # a counter jump is refused too: exact, never a window
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, uid, ahead)
    assert exc.value.code == "ticket.key_not_next"


def test_a_viewer_creation_is_role_denied_before_the_key_and_leaks_no_next_key(env):
    viewer = Person()
    env.store.append(
        env.person_event(env.owner, "workspace", "member.added", person=viewer.ref, name="Vi", role="viewer",
                         pk_pub=crypto.b64u(viewer.pk_pub), device_cert=viewer.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    phone = Phone(env, viewer)
    ev = creation(env, phone, new_ulid(), key="DEMO-0099", owner=viewer.ref)
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, new_ulid(), ev)
    assert exc.value.code == "role.denied" and exc.value.detail is None
    assert "detail" not in exc.value.error() and env.store.peek_key() not in str(exc.value)
    stranger = Phone.__new__(Phone)  # a signer that is not a member at all (never added to the roster)
    stranger.p, stranger.key = Person(), crypto.generate_private_key()
    stranger.device = certs.device_ref(crypto.public_bytes(stranger.key))
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, new_ulid(), creation(env, stranger, new_ulid(), owner=stranger.p.ref))
    assert exc.value.code == "role.denied" and exc.value.detail is None


def test_a_members_device_without_decide_is_role_denied_with_no_detail(env):
    import types
    from contextlib import nullcontext

    ws = env.store.state.workspace
    dev = dataclasses.replace(ws.devices[next(iter(ws.devices))], scopes=("look",))  # no certificate can omit decide
    owner = ws.members[env.owner.ref]
    fake = types.SimpleNamespace(
        locked=nullcontext,
        ticket=lambda *a, **k: None,
        peek_key=lambda: "DEMO-0001",
        state=types.SimpleNamespace(
            workspace=types.SimpleNamespace(members={env.owner.ref: owner}, devices={dev.id: dev}),
            revocation_evidence=lambda: set(),
        ),
    )
    ev = {"type": "ticket.created", "owner": env.owner.ref, "key": "DEMO-0099",
          "actor": {"kind": "person", "id": dev.person, "device": dev.id}}  # fmt: skip
    with pytest.raises(HostError) as exc:
        admission.check_target(fake, new_ulid(), ev)
    assert exc.value.code == "role.denied" and exc.value.detail is None and "detail" not in exc.value.error()


def test_a_removed_or_unknown_device_is_role_denied_with_no_detail(env):
    phone = Phone(env, env.owner)
    ghost = Phone.__new__(Phone)  # a device the log never saw
    ghost.p, ghost.key = env.owner, crypto.generate_private_key()
    ghost.device = certs.device_ref(crypto.public_bytes(ghost.key))
    env.store.append(env.person_event(env.owner, "workspace", "device.removed", device=phone.device), log="workspace")
    for bad in (phone, ghost):
        ev = creation(env, bad, new_ulid(), key="DEMO-0099")
        with pytest.raises(HostError) as exc:
            admission.check_target(env.store, new_ulid(), ev)
        assert exc.value.code == "role.denied" and exc.value.detail is None
        assert "detail" not in exc.value.error()


def test_a_non_creator_targeting_an_existing_uid_is_role_denied_not_ticket_exists(env):
    uid = env.new_ticket()
    viewer = Person()
    env.store.append(
        env.person_event(env.owner, "workspace", "member.added", person=viewer.ref, name="Vi", role="viewer",
                         pk_pub=crypto.b64u(viewer.pk_pub), device_cert=viewer.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    ev = creation(env, Phone(env, viewer), uid, owner=viewer.ref)
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, uid, ev)
    assert exc.value.code == "role.denied" and exc.value.detail is None and "detail" not in exc.value.error()


def test_check_target_of_a_creation_refuses_a_used_uid_and_another_owner(env, phone):
    uid = env.new_ticket()
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, uid, creation(env, phone, uid))
    assert exc.value.code == "ticket.exists"
    other = creation(env, phone, new_ulid(), owner="p_" + "0" * 32)
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, new_ulid(), other)
    assert exc.value.code == "invalid.input"


def test_next_key_is_the_stores_exact_next_key(env):
    assert admission.next_key(env.store) == env.store.peek_key() == "DEMO-0001"
    env.new_ticket()
    assert admission.next_key(env.store) == "DEMO-0002"


# --- P6: the freshness window ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset_ms,ok",
    [(-900_000, True), (-900_001, False), (300_000, True), (300_001, False), (0, True)],
)
def test_the_window_is_now_minus_900_s_to_now_plus_300_s(offset_ms, ok):
    now = 1_791_626_400.0
    eid = new_ulid(int(now * 1000) + offset_ms)
    if ok:
        admission.check_window(eid, now)
    else:
        with pytest.raises(HostError) as exc:
            admission.check_window(eid, now)
        assert exc.value.code == "event.expired" and (exc.value.exit, exc.value.retryable) == (3, False)


def test_the_window_reads_the_ulid_time_exactly():
    assert admission.ulid_ms(new_ulid(1_791_626_400_123)) == 1_791_626_400_123
    with pytest.raises(HostError) as exc:
        admission.check_window("not-a-ulid", 0.0)
    assert exc.value.code == "validation.event"


def test_column_r_auth_values():
    assert admission.COLUMN_R_AUTH == frozenset({"secure-enclave-unlocked", "passphrase"})


# --- amendment A (15 Oct 2026): a known identity is answered before the freshness window ---------------------------


def test_a_landed_event_is_committed_even_after_its_window(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    seq = env.store.append(ev, log=uid).event["seq"]
    late = env.clock[0] + 3600  # far past the window: freshness never turns a committed write into a re-review
    assert admission.known_or_fresh(env.store, uid, ev, late) == seq


def test_known_or_fresh_refuses_a_known_id_with_other_content_before_the_window(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    env.store.append(ev, log=uid)
    with pytest.raises(HostError) as exc:
        admission.known_or_fresh(env.store, uid, {**ev, "option": "api"}, env.clock[0] + 3600)
    assert exc.value.code == "event.duplicate_id"


def test_only_a_never_seen_event_gets_the_window(env, phone):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    assert admission.known_or_fresh(env.store, uid, ev, env.clock[0]) is None
    with pytest.raises(HostError) as exc:
        admission.known_or_fresh(env.store, uid, ev, env.clock[0] + 901)
    assert exc.value.code == "event.expired"


# --- review #474: the log is a ticket uid; never the workspace log in check_target --------------------------------


@pytest.mark.parametrize("bad", ["DEMO-0001", "1", "#1", "01j9zp0000000000000000000t", "", None])
def test_the_helpers_refuse_a_log_that_is_not_a_uid(env, phone, bad):
    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    for call in (
        lambda: admission.lookup(env.store, bad, ev["id"]),
        lambda: admission.dedup(env.store, bad, ev),
        lambda: admission.known_or_fresh(env.store, bad, ev, env.clock[0]),
        lambda: admission.check_target(env.store, bad, ev),
    ):
        with pytest.raises(HostError) as exc:
            call()
        assert exc.value.code == "validation.event"


def test_check_target_refuses_the_workspace_log(env, phone):
    ev = phone.event(env, "workspace", "settings.changed", set={"grant_hours": 4})
    with pytest.raises(HostError) as exc:
        admission.check_target(env.store, "workspace", ev)
    assert exc.value.code == "validation.event"
    assert admission.lookup(env.store, "workspace", ev["id"]) is None  # lookup and dedup may read the workspace log


# --- H12e-2: device standing for column R (I2) and judge_events never leaks a model exception (I3b) -----------------


def test_device_standing_follows_the_f1_log(env, phone):
    """R2's ``standing()`` must use this: a REAL ``device.removed`` through the normal path ends the standing."""
    s = env.store
    with s.locked():
        assert admission.device_standing(s, env.owner.ref, phone.device) == "ok"
        assert admission.device_standing(s, env.owner.ref, env.owner.device) == "ok"
        assert admission.device_standing(s, env.owner.ref, "d_" + "A" * 43) == "unknown"
        assert admission.device_standing(s, "p_" + "B" * 20, phone.device) == "unknown"  # another person's device
    s.append(env.person_event(env.owner, "workspace", "device.removed", device=phone.device), log="workspace")
    with s.locked():
        assert admission.device_standing(s, env.owner.ref, phone.device) == "removed"
        assert admission.device_standing(s, env.owner.ref, env.owner.device) == "ok"


def _rollback(env: Env, backup: Any) -> None:
    for name in ("events", "tickets", "config.json", "keys.jsonl"):  # logs back, ``.state`` kept
        target = env.store_dir / name
        shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        src = backup / ".orch" / name
        shutil.copytree(src, target) if src.is_dir() else shutil.copy(src, target)


def test_device_standing_of_a_revoked_device(env):
    d2, _ = env.add_device()
    s = env.store
    assert admission.device_standing(s, env.owner.ref, d2) == "ok"
    env.revoke_device(d2)
    assert admission.device_standing(s, env.owner.ref, d2) == "removed"


def test_device_standing_after_member_removed(env):
    other = Person()
    s = env.store
    s.append(
        env.person_event(env.owner, "workspace", "member.added", person=other.ref, name="Mara", role="member",
                         pk_pub=crypto.b64u(other.pk_pub), device_cert=other.cert(created=env.clock[0] * 1000)),
        log="workspace",
    )  # fmt: skip
    assert admission.device_standing(s, other.ref, other.device) == "ok"
    s.append(env.person_event(env.owner, "workspace", "member.removed", person=other.ref), log="workspace")
    assert admission.device_standing(s, other.ref, other.device) == "removed"
    assert admission.device_standing(s, env.owner.ref, env.owner.device) == "ok"


def test_device_standing_of_a_tombstone_only_device(env, tmp_path):
    """A revocation the restored chain never had stays as a tombstone: this person's → removed, another's → unknown."""
    s = env.store
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    d2, _ = env.add_device()
    env.revoke_device(d2)
    s.close()
    _rollback(env, backup)
    s = env.open()
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    env.store = s
    assert d2 not in s.state.workspace.devices  # a tombstone only
    assert admission.device_standing(s, env.owner.ref, d2) == "removed"
    assert admission.device_standing(s, "p_" + "B" * 20, d2) == "unknown"


def test_a_restore_that_cuts_a_device_removed_revives_the_device(env, tmp_path):
    """Documented F1 behaviour: a restore abandons the removal, so the device is ``ok`` again. A lost phone must be
    REVOKED (a revocation survives a restore), not merely removed."""
    d2, _ = env.add_device()
    s = env.store
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    env.store = s
    s.append(env.person_event(env.owner, "workspace", "device.removed", device=d2), log="workspace")
    assert admission.device_standing(s, env.owner.ref, d2) == "removed"
    s.close()
    _rollback(env, backup)
    s = env.open()
    env.store = s
    s.append(
        env.person_event(env.owner, "workspace", "restore", **s.restore_facts("workspace"), reason="rollback"),
        log="workspace",
    )
    assert admission.device_standing(s, env.owner.ref, d2) == "ok"


def test_judge_events_maps_a_model_exception_to_validation_event(env, phone):
    """A signed but malformed event (an unknown ticket type) made ``model.preview`` raise ``KeyError``."""
    uid = new_ulid()
    ev = phone.event(
        env, uid, "ticket.created", based_on=None, key=env.store.peek_key(), ticket_type="no-such-type", title="x",
        owner=env.owner.ref,
    )  # fmt: skip
    with pytest.raises(HostError) as exc, env.store.locked():
        judge_events(env.store, uid, [ev])
    assert exc.value.code == "validation.event"


def test_judge_events_maps_other_exception_types(env, phone, monkeypatch):
    import orch.model as model

    uid = env.new_ticket()
    h = ask(env, uid, options=OPTS)
    ev = phone.event(env, uid, "question.answered", question="Q1", hash=h, option="csv")
    for exc_type in (KeyError, TypeError, ValueError, AttributeError):

        def boom(*a: Any, _t: Any = exc_type, **k: Any) -> Any:
            raise _t("boom")

        monkeypatch.setattr(model, "preview", boom)
        with pytest.raises(HostError) as exc, env.store.locked():
            judge_events(env.store, uid, [ev])
        assert exc.value.code == "validation.event"
