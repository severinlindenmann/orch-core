"""R2: the host side of the relay entry (``orch/host/relayentry.py``) on a real served workspace: column R through
``Call.authorize``, ``relay.submit`` in one store-lock span (dedup by event identity, the next key, ``judge_events``,
``outcomes.plan``, ``append_relay``), the standing of a pair, and crash reconciliation by event identity."""

from __future__ import annotations

from typing import Any

import pytest

from orch import canon, crypto
from orch.host.errors import HostError
from orch.host.outcomes import BUILDERS
from orch.host.relayentry import RelayEntry, rebuild_relay_submit, register, same_event
from orch.relay.framing import Identity, Refusal
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures

from .bridgekit import ulid


def _ident(served, person) -> Identity:  # noqa: F811
    return Identity(served.wid, person.device, person.ref, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64)


def _entry(served) -> RelayEntry:  # noqa: F811
    from orch.host.dispatch import RegistryBackend

    register(served.service)
    return RelayEntry(served.service, RegistryBackend())


def _on_worker(served, fn) -> Any:  # noqa: F811
    return served.service.submit(fn).result(60)


def _event(served, person, log: str, typ: str, *, eid: str | None = None, **fields) -> dict:  # noqa: F811
    store = served.service.store

    def fresh():
        with store.locked():
            return store.state, store.log_head(log)

    st, head = _on_worker(served, fresh)
    e = {
        "v": 2, "id": eid or ulid(int(served.lab.ws.clock[0] * 1000)), "type": typ,
        "actor": {"kind": "person", "id": person.ref, "device": person.device}, "auth": "passphrase", "hash_v": 1,
        "roster_v": st.workspace.roster_v, "based_on": head, **fields,
    }  # fmt: skip
    e["sig"] = crypto.b64u(person.dk_sign(canon.person_signing_bytes(served.wid, log, e)))
    return e


def _next_key(served) -> str:  # noqa: F811
    return _on_worker(served, lambda: served.service.store._next_key())  # noqa: SLF001 - test oracle


def _new_ticket(served, person, *, key=None, log=None, title="From the phone") -> tuple[str, dict]:  # noqa: F811
    log = log or ulid(int(served.lab.ws.clock[0] * 1000))
    ev = _event(served, person, log, "ticket.created", key=key or _next_key(served), ticket_type="bug", title=title,
                owner=person.ref)  # fmt: skip
    return log, ev


def _submit(served, entry, person, log, ev, *, recheck=lambda: True, idem=None):  # noqa: F811
    ident = _ident(served, person)
    return _on_worker(served, lambda: entry.submit(ident, idem or ev["id"], "new_ticket", log, [ev], recheck))


def _code(fn) -> str:
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


def test_standing_of_a_pair(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    assert _on_worker(served, lambda: entry.standing(owner.device, owner.ref)) == "ok"
    assert _on_worker(served, lambda: entry.standing("d_" + "1" * 32, owner.ref)) == "not_member"
    assert _on_worker(served, lambda: entry.standing(owner.device, "p_" + "2" * 32)) == "not_member"
    p = served.lab.add_member()
    assert _on_worker(served, lambda: entry.standing(p.device, p.ref)) == "ok"
    served.lab.revoke_device(p)
    assert _on_worker(served, lambda: entry.standing(p.device, p.ref)) == "revoked"


def test_a_read_is_column_r_with_principal_r_device(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    got = _on_worker(served, lambda: entry.read(_ident(served, owner), "whoami", {}, lambda: True))
    assert got["data"]["peer"] == {"class": "relay", "uid": -1} and got["data"]["person"] == owner.ref


def test_a_read_whose_identity_no_longer_holds_is_refused(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    assert _code(
        lambda: _on_worker(served, lambda: entry.read(_ident(served, owner), "whoami", {}, lambda: False))
    ) == ("op.not_allowed")


def test_a_socket_only_op_is_refused_in_column_r(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    code = _code(
        lambda: _on_worker(served, lambda: entry.read(_ident(served, owner), "session.list", {}, lambda: True))
    )
    assert code == "op.not_allowed"


def test_new_ticket_commits_once_and_an_identical_retry_gets_the_stored_outcome(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    first = _submit(served, entry, owner, log, ev)
    assert first["data"]["log"] == log and first["duplicate"] is False
    again = _submit(served, entry, owner, log, ev)
    assert again["duplicate"] is True and again["data"] == first["data"]
    # the same event signed again (ECDSA: other signature bytes) is the same event identity
    resigned = {**ev, "sig": crypto.b64u(owner.dk_sign(canon.person_signing_bytes(served.wid, log, ev)))}
    assert resigned["sig"] != ev["sig"]
    assert _submit(served, entry, owner, log, resigned)["duplicate"] is True
    assert served.service.store.logged_event(log, ev["id"]) is not None


def test_an_idem_reused_for_other_content_is_idem_conflict(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    _submit(served, entry, owner, log, ev)
    other = _event(served, owner, log, "ticket.created", eid=ev["id"], key=ev["key"], ticket_type="bug",
                   title="other title", owner=owner.ref)  # fmt: skip
    assert _code(lambda: _submit(served, entry, owner, log, other)) == "idem.conflict"


def test_the_same_event_id_from_another_device_is_event_duplicate_id(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    p = served.lab.add_member()
    log, ev = _new_ticket(served, owner)
    _submit(served, entry, owner, log, ev)
    theirs = _event(served, p, log, "ticket.created", eid=ev["id"], key=ev["key"], ticket_type="bug",
                    title=ev["title"], owner=p.ref)  # fmt: skip
    assert _code(lambda: _submit(served, entry, p, log, theirs)) == "event.duplicate_id"


def test_a_key_that_is_not_next_is_key_not_next_with_the_advisory_key(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    want = _next_key(served)
    prefix, n = want.rsplit("-", 1)
    log, ev = _new_ticket(served, owner, key=f"{prefix}-{int(n) + 5}")
    with pytest.raises(Refusal) as e:
        _submit(served, entry, owner, log, ev)
    assert e.value.code == "ticket.key_not_next" and e.value.fields["next_key"] == want
    assert served.service.store.logged_event(log, ev["id"]) is None


def test_a_new_ticket_in_an_existing_log_is_ticket_exists(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    uid, _key = served.ticket()
    log, ev = _new_ticket(served, owner, log=uid)
    assert _code(lambda: _submit(served, entry, owner, log, ev)) == "ticket.exists"


def test_an_answer_to_a_missing_ticket_is_not_found(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log = ulid(int(served.lab.ws.clock[0] * 1000))
    ev = _event(served, owner, log, "question.answered", question="q_" + ulid(), hash="sha256:" + "0" * 64,
                option="yes")  # fmt: skip
    ident = _ident(served, owner)
    assert _code(
        lambda: _on_worker(served, lambda: entry.submit(ident, ev["id"], "answer", log, [ev], lambda: True))
    ) == ("not_found")


def test_a_submit_whose_identity_no_longer_holds_appends_nothing(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    assert _code(lambda: _submit(served, entry, owner, log, ev, recheck=lambda: False)) == "op.not_allowed"
    assert served.service.store.logged_event(log, ev["id"]) is None


def test_the_model_judges_before_anything_lands(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    bad = _event(served, owner, log, "ticket.created", key=ev["key"], ticket_type="bug", title="t",
                 owner="p_" + "9" * 32)  # fmt: skip
    code = _code(lambda: _submit(served, entry, owner, log, bad))
    assert code not in ("committed",) and served.service.store.logged_event(log, bad["id"]) is None


def test_crash_reconciliation_matches_event_identity_not_the_planned_id_alone(served):  # noqa: F811
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    rebuild = BUILDERS["relay.submit"]
    assert rebuild is rebuild_relay_submit  # registered at import of orch.host.outcomes, never by register() (M1)
    record = {"namespace": served.wid, "args_normalised": {"log": log, "events": [{k: v for k, v in ev.items()
                                                                                    if k != "sig"}]}}  # fmt: skip
    logged = {**ev, "seq": 1}
    assert rebuild(record, [logged])["data"] == {"log": log, "seqs": [1]}
    with pytest.raises(HostError) as e:
        rebuild(record, [{**logged, "title": "someone else's"}])
    assert e.value.code == "event.duplicate_id"


def test_the_builder_takes_the_workspace_from_the_record_not_the_last_registered_one(served):  # noqa: F811
    """M1 / D2-7: one process, several workspaces: the builder keeps no workspace of its own (it reads the record's
    ``namespace``), so registering another service changes nothing; a record without a namespace is ``internal``."""
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    events = [{k: v for k, v in ev.items() if k != "sig"}]
    logged = {**ev, "seq": 1}
    register(served.service)
    assert BUILDERS["relay.submit"] is rebuild_relay_submit
    ok = {"namespace": served.wid, "args_normalised": {"log": log, "events": events}}
    assert rebuild_relay_submit(ok, [logged])["data"]["seqs"] == [1]
    with pytest.raises(HostError) as e:
        rebuild_relay_submit(ok, [{**logged, "title": "other"}])
    assert e.value.code == "event.duplicate_id"
    with pytest.raises(HostError) as e:
        rebuild_relay_submit({"args_normalised": ok["args_normalised"]}, [logged])
    assert e.value.code == "internal"


def test_same_event_ignores_signature_bytes_and_host_fields(served):  # noqa: F811
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    assert same_event(served.wid, log, ev, {**ev, "sig": "x", "seq": 4, "at": "t", "prev": None})
    assert not same_event(served.wid, log, ev, {**ev, "title": "changed"})
    assert not same_event(served.wid, log, ev, {**ev, "actor": {**ev["actor"], "device": "d_" + "0" * 32}})


def test_an_event_the_model_cannot_judge_is_validation_event_and_never_lands(served):  # noqa: F811
    """I3b: the real ``judge_events`` lets a model exception (KeyError on an unknown type) escape; relayentry maps it
    to a definite ``validation.event`` (the bridge's P3 refuses the same event before the lock), never a stuck
    write, and the outcome is terminal (a second submit replays it)."""
    entry = _entry(served)
    owner = served.lab.ws.owner
    log = ulid(int(served.lab.ws.clock[0] * 1000))
    ev = _event(served, owner, log, "ticket.created", key=_next_key(served), ticket_type="no-such-type", title="t",
                owner=owner.ref)  # fmt: skip
    assert _code(lambda: _submit(served, entry, owner, log, ev)) == "validation.event"
    assert served.service.store.logged_event(log, ev["id"]) is None
    assert _code(lambda: _submit(served, entry, owner, log, ev)) == "validation.event"


def _remove_device(lab_, p) -> None:
    """A real F1 ``device.removed`` (signed by the owner) appended by another writer (the CLI)."""
    ws = lab_.ws
    ws.tick()
    store = ws.other()
    ws.store = store
    try:
        store.append(ws.person_event(ws.owner, "workspace", "device.removed", device=p.device), log="workspace")
    finally:
        store.close()
        ws.store = None


def test_a_removed_device_is_not_member_for_reads_and_writes(served):  # noqa: F811
    """I2 / 14.8 "a CLI device.removed between P7 and L2 -> refused": the device stays in ``ws.devices`` with
    ``removed``; ``standing()`` asks the F1 device standing and answers ``not_member``. The bridge's recheck (inside
    the op's own job) then refuses a read and a write: nothing lands."""
    entry = _entry(served)
    p = served.lab.add_member()
    assert _on_worker(served, lambda: entry.standing(p.device, p.ref)) == "ok"
    log, ev = _new_ticket(served, p)
    _remove_device(served.lab, p)
    assert _on_worker(served, lambda: entry.standing(p.device, p.ref)) == "not_member"

    def recheck() -> bool:
        return entry.standing(p.device, p.ref) == "ok"

    assert (
        _code(lambda: _on_worker(served, lambda: entry.read(_ident(served, p), "whoami", {}, recheck)))
        == "op.not_allowed"
    )
    assert _code(lambda: _submit(served, entry, p, log, ev, recheck=recheck)) == "op.not_allowed"
    assert served.service.store.logged_event(log, ev["id"]) is None


def test_a_removed_members_device_is_not_member(served):  # noqa: F811
    entry = _entry(served)
    p = served.lab.add_member()
    served.lab.remove_member(p)
    assert _on_worker(served, lambda: entry.standing(p.device, p.ref)) == "not_member"
