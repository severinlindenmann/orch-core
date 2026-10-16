"""§11A.17.4 on a real served workspace: column R accepts a convenience-signed answer only on a dev host's sandbox
workspace (L2 fresh dev gate, L8 token plus a fresh gate), writes the permanent mark, requires reader level 1, carries
``whoami.relay.dev``; every other workspace and host answers exactly as before."""

from __future__ import annotations

from typing import Any

import pytest

from orch import canon, crypto
from orch.host import convenience, devmode
from orch.host.errors import HostError
from orch.host.relayentry import RelayEntry, register
from orch.identity.reader import decl_digest
from orch.ops.readers import convenience_seen
from orch.relay.framing import Identity, Refusal
from tests.host.devgate_helpers import SbxLab
from tests.host.dispatch_helpers import Served, ok
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import Lab
from tests.identity.reader_helpers import decl
from tests.relay.bridgekit import ulid


@pytest.fixture
def dev_served(tmp_path):
    lab = SbxLab(tmp_path)
    served = Served(lab, dev=lab.dev())
    try:
        yield served
    finally:
        served.close()
        lab.close()


@pytest.fixture
def normal_served(tmp_path):
    (tmp_path / "normal").mkdir()
    lab = Lab(tmp_path / "normal")
    served = Served(lab)
    try:
        yield served
    finally:
        served.close()
        lab.close()


def _on_worker(served, fn) -> Any:
    return served.service.submit(fn).result(60)


def _entry(served) -> RelayEntry:
    from orch.host.dispatch import RegistryBackend

    register(served.service)
    return RelayEntry(served.service, RegistryBackend())


def _ident(served, rd: str | None = None) -> Identity:
    o = served.lab.ws.owner
    return Identity(served.wid, o.device, o.ref, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64, rd)


def _declare(served, entry) -> str:
    """The phone's level-1 reader declaration (reader.declare, served without rd), its digest for ``rd``."""
    owner = served.lab.ws.owner
    d = decl(
        owner, served.wid, level=1, via="declare", created_ms=int(served.service.clock() * 1000), surfaces=("view",)
    )
    got = _on_worker(served, lambda: entry.read(_ident(served), "reader.declare", {"reader_decl": d}, lambda: True))
    assert got["data"]["digest"] == decl_digest(d)
    return got["data"]["digest"]


def _question(served, text: str = "deploy?") -> tuple[str, dict[str, Any]]:
    """A ticket with an open question (an agent asks it); returns the log and the answer's fields. One question
    text is answered once (its hash): each test answer asks its own."""
    uid, key = served.ticket(f"About: {text}")
    Human(served).agent("ask", {"ref": key, "text": text, "options": ["yes", "no"]})

    def read() -> Any:
        store = served.service.store
        with store.locked():
            view = store.ticket(uid)
            return view.questions[0], view.fields["questions"][0], store.log_head(uid)

    q, doc, head = _on_worker(served, read)
    return uid, {"question": q.id, "hash": q.hash, "option": doc["options"][0]["key"], "based_on": head}


def _answer(served, log: str, fields: dict[str, Any], *, auth: str = "convenience") -> dict[str, Any]:
    owner = served.lab.ws.owner

    def build() -> dict[str, Any]:
        store = served.service.store
        with store.locked():
            roster = store.state.workspace.roster_v
        e = {
            "v": 2, "id": ulid(int(served.lab.ws.clock[0] * 1000)), "type": "question.answered",
            "actor": {"kind": "person", "id": owner.ref, "device": owner.device}, "auth": auth, "hash_v": 1,
            "roster_v": roster, **fields,
        }  # fmt: skip
        e["sig"] = crypto.b64u(owner.dk_sign(canon.person_signing_bytes(served.wid, log, e)))
        return e

    return _on_worker(served, build)


def _submit(served, entry, log, ev, rd=None) -> dict[str, Any]:
    ident = _ident(served, rd)
    return _on_worker(served, lambda: entry.submit(ident, ev["id"], "answer", log, [ev], lambda: True))


def _code(fn) -> str:
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


def test_first_dev_convenience_answer_writes_mark(dev_served):
    entry = _entry(dev_served)
    rd = _declare(dev_served, entry)
    log, fields = _question(dev_served)
    assert not convenience_seen(dev_served.lab.state, dev_served.wid)
    ev = _answer(dev_served, log, fields)
    res = _submit(dev_served, entry, log, ev, rd)
    assert res["duplicate"] is False and dev_served.service.store.logged_event(log, ev["id"])["auth"] == "convenience"
    assert convenience_seen(dev_served.lab.state, dev_served.wid)


def test_secure_enclave_and_passphrase_unchanged_on_dev_host(dev_served):
    entry = _entry(dev_served)
    rd = _declare(dev_served, entry)
    for auth in ("passphrase", "secure-enclave-unlocked"):
        log, fields = _question(dev_served, f"deploy with {auth}?")
        ev = _answer(dev_served, log, fields, auth=auth)
        assert _submit(dev_served, entry, log, ev, rd)["duplicate"] is False
    assert not convenience_seen(dev_served.lab.state, dev_served.wid)


def test_dev_gate_sets_required_reader(dev_served, normal_served):
    from orch.host.readerops import read_required

    assert _on_worker(dev_served, lambda: read_required(dev_served.service)) == 1
    assert _on_worker(normal_served, lambda: read_required(normal_served.service)) == 0
    entry = _entry(dev_served)
    with pytest.raises(Refusal) as e:  # a request without rd: no_declaration, already for whoami
        _on_worker(dev_served, lambda: entry.read(_ident(dev_served), "whoami", {}, lambda: True))
    assert (e.value.code, e.value.fields.get("reason")) == ("sign.convenience_readers", "no_declaration")
    # a level-0 phone pairing is refused at the pair gate (required_reader 1)
    assert _on_worker(dev_served, lambda: convenience._required(dev_served.service, history=False)) == 1


def test_whoami_relay_dev_only_on_dev_gated_workspace(dev_served, normal_served, monkeypatch):
    from orch.relay import worker as relay_worker

    dev_entry, normal_entry = _entry(dev_served), _entry(normal_served)
    rd = _declare(dev_served, dev_entry)
    id8 = dev_served.lab.sandbox_id[:8]
    for ready in (False, True):
        monkeypatch.setattr(relay_worker, "R6_READY", ready)
        got = _on_worker(dev_served, lambda: dev_entry.read(_ident(dev_served, rd), "whoami", {}, lambda: True))
        assert got["data"]["relay"] == {
            "ops": {"answer": 1, "new_ticket": 1}, "r6_ready": ready, "dev": {"sim_convenience": True, "sandbox": id8},
        }  # fmt: skip
        got = _on_worker(normal_served, lambda: normal_entry.read(_ident(normal_served), "whoami", {}, lambda: True))
        assert got["data"]["relay"] == {"ops": {"answer": 1, "new_ticket": 1}, "r6_ready": ready}
    # the gate goes away (ORCH_NO_CONVENIENCE): dev is gone at the next read, never cached
    monkeypatch.setenv("ORCH_NO_CONVENIENCE", "1")
    got = _on_worker(dev_served, lambda: dev_entry.read(_ident(dev_served, rd), "whoami", {}, lambda: True))
    assert "dev" not in got["data"]["relay"]


def test_host_status_dev_absent_on_normal_host(normal_served):
    status = ok(normal_served.client().req("host.status"))["data"]
    assert set(status) == {"version", "tier", "state", "uptime_s"}
    listed = ok(normal_served.control.call("host.list"))["data"]
    assert set(listed) == {"workspaces"} and all("sandbox" not in row for row in listed["workspaces"])


def test_real_workspace_convenience_submit_answers_op_not_allowed(normal_served):
    """On a normal host (and for every real workspace) a convenience answer that reached the host is refused at L2:
    the gate is None; nothing lands, no mark."""
    entry = _entry(normal_served)
    assert devmode.dev_gate(normal_served.service) is None
    log, fields = _question(normal_served)
    ev = _answer(normal_served, log, fields)
    assert _code(lambda: _submit(normal_served, entry, log, ev)) == "op.not_allowed"
    assert normal_served.service.store.logged_event(log, ev["id"]) is None
    assert not convenience_seen(normal_served.lab.state, normal_served.wid)


def test_dev_gate_rechecked_under_lock(dev_served):
    """S4 broken between P5 (the cached gate still says yes) and L2: a member.added lands first; L2 refuses."""
    from tests.identity.helpers import Person

    entry = _entry(dev_served)
    rd = _declare(dev_served, entry)
    log, fields = _question(dev_served)
    assert devmode.gated(dev_served.service) is True
    ws, p = dev_served.lab.ws, Person()

    def add(store: Any) -> None:
        ws.store = store
        store.append(
            ws.person_event(ws.owner, "workspace", "member.added", person=p.ref, name="q", role="member",
                            pk_pub=crypto.b64u(p.pk_pub), device_cert=p.cert(created=ws.clock[0] * 1000)),
            log="workspace",
        )  # fmt: skip

    dev_served.append(add)
    ev = _answer(dev_served, log, fields)
    assert _code(lambda: _submit(dev_served, entry, log, ev, rd)) == "op.not_allowed"
    assert dev_served.service.dev_ok is False and devmode.gated(dev_served.service) is False
    assert dev_served.service.store.logged_event(log, ev["id"]) is None


def test_dev_gate_needs_token_and_fresh_call(dev_served, monkeypatch):
    from orch.host.human import append_relay

    service = dev_served.service
    log, fields = _question(dev_served)
    ev = _answer(dev_served, log, fields)
    owner = dev_served.lab.ws.owner
    gate = _on_worker(dev_served, lambda: devmode.dev_gate(service))
    assert gate is not None

    def append(token: Any) -> Any:
        return _on_worker(
            dev_served, lambda: append_relay(service, log, [ev], [{}], person_id=owner.ref, device_id=owner.device,
                                             dev_gate=token),
        )  # fmt: skip

    for token in (None, devmode.DevGate("0" * 32, service.workspace_id), devmode.DevGate(gate.sandbox_id, "f" * 32)):
        landed, error = append(token)
        assert landed == [] and error.code == "op.not_allowed"
    monkeypatch.setenv("ORCH_NO_CONVENIENCE", "1")  # the token alone: the fresh call at the append fails
    landed, error = append(gate)
    assert landed == [] and error.code == "op.not_allowed"
    monkeypatch.delenv("ORCH_NO_CONVENIENCE")
    landed, error = append(gate)
    assert error is None and len(landed) == 1


def test_dev_host_direct_mode_convenience_still_refused(dev_served):
    """The person path (human.finish's append, the dashboard's click signer) on a dev host: dev_host, always."""
    from orch.host.human import append_signed

    service = dev_served.service
    log, fields = _question(dev_served)
    ev = _answer(dev_served, log, fields)
    landed, error = _on_worker(dev_served, lambda: append_signed(service, log, [ev], [{}], signed_by=None))
    assert landed == [] and error.code == "sign.convenience_unavailable" and error.detail["reason"] == "dev_host"
    owner = dev_served.lab.ws.owner
    with pytest.raises(HostError) as e:
        _on_worker(dev_served, lambda: convenience.check(service, owner.ref, owner.device))
    assert e.value.detail["reason"] == "dev_host"


def test_worker_restart_with_convenience_history_on_level1_relay(dev_served):
    """After a dev convenience answer, the worker's start check passes when the relay's level is 1 (and refuses at
    level 0, as for any workspace with convenience history)."""
    from orch.host.relayhook import convenience_block
    from orch.store.readerrecs import write_relay_level

    entry = _entry(dev_served)
    rd = _declare(dev_served, entry)
    log, fields = _question(dev_served)
    _submit(dev_served, entry, log, _answer(dev_served, log, fields), rd)
    service = dev_served.service
    from orch.host.relayr6 import _convenience_block

    write_relay_level(dev_served.lab.state, dev_served.wid, service.host_uid, 1, 1)
    assert _on_worker(dev_served, lambda: convenience_block(service)) is None
    assert _on_worker(dev_served, lambda: _convenience_block(service)) is None  # R6's copy reads the level too
    write_relay_level(dev_served.lab.state, dev_served.wid, service.host_uid, 0, 2)
    assert _on_worker(dev_served, lambda: convenience_block(service)) == "convenience_events_present"
    assert _on_worker(dev_served, lambda: _convenience_block(service)) == "convenience_events_present"


def test_the_gate_counts_as_the_flag_for_reader_checks(dev_served):
    """flag_or_gate everywhere the flag decides required_reader: a reader op is refused while the gate passes, and
    observe_relay_level alarms a level-0 relay on a gated workspace (the worker is not halted: the alarm is the
    rule)."""
    from orch.host.relaylink import link_path, observe_relay_level

    service = dev_served.service
    with pytest.raises(HostError) as e:
        _on_worker(dev_served, lambda: convenience.refuse_reader_op(service, "member.add"))
    assert e.value.code == "sign.convenience_readers"
    path = link_path(dev_served.lab.state, dev_served.wid)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    from orch.host.custody import write_private

    write_private(path, {"origin": "https://relay.dev.example"})
    alarms: list[tuple[str, str]] = []
    observe_relay_level(
        dev_served.lab.state, dev_served.wid, service.host_uid, 0, lambda c, d: alarms.append((c, d)),
        origin="https://relay.dev.example", gated=lambda: devmode.gated(service),
    )  # fmt: skip
    assert [c for c, _ in alarms] == ["relay_reader_old"]


def test_dev_host_link_to_other_origin_refused(dev_served):
    from orch.host.relayhook import reserve_link

    service = dev_served.service
    with pytest.raises(HostError) as e:
        _on_worker(
            dev_served, lambda: reserve_link(service, origin="https://relay.example", max_publication_bytes=1 << 22)
        )
    assert e.value.code == "host.dev_refused" and e.value.detail["reason"] == "dev_origin"
    from orch.host.relaylink import link_path

    assert not link_path(dev_served.lab.state, dev_served.wid).exists()


def test_dev_host_records_push_refused(dev_served):
    from orch.host import records

    ctx = records.Ctx(
        store=dev_served.service.store, state_dir=dev_served.lab.state, workspace_id=dev_served.wid,
        host_uid=dev_served.service.host_uid, clock=dev_served.service.clock, principal="uid:0",
        person=dev_served.lab.owner, sees=lambda v: True, holder=dev_served.service,
    )  # fmt: skip
    args = {
        "sha": "0" * 40,
        "url": "https://example.invalid/r.git",
        "ref": "refs/heads/main",
        "history_convenience": False,
    }
    with pytest.raises(HostError) as e:
        _on_worker(dev_served, lambda: records._export_check(ctx, args))
    assert e.value.code == "host.dev_refused" and e.value.detail["reason"] == "dev_host"
