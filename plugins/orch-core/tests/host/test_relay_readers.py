"""H12d-3 (orch-v2-host §0.8.6 item 9.4, 9.8, 9.9): the relay as a reader. Its level from ``GET /v2/relay-info`` is
stored with the link (``relay/reader.json``) at link step 1 and at every R1 login; ``relay.link`` follows the relaxed
rule (a level-1 relay is linked with convenience history and while the flag is on); ``relay_paired`` counts only a
relay below level 1; a relay whose level drops alarms ``relay_reader_old``; ``relay.pair`` has no early refusal at
the offer (the phone's declaration is gated at step 4 and in the allocation)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from orch.host.errors import HostError

UID = os.getuid()
WS = "e" * 32


class ConvStore:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []

    @contextlib.contextmanager
    def locked(self):
        yield

    def merged_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [("workspace", e) for e in self.events]


class LinkService:
    def __init__(self, state: Path, store: Any, ws: str = WS) -> None:
        self.state_dir, self.workspace_id, self.host_uid, self.store = state, ws, UID, store


CONV = [{"type": "x", "auth": "convenience"}]


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    return state


def _relay(state: Path, ws: str = WS) -> Path:
    d = state / "hosts" / ws / "relay"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _link(state: Path, ws: str = WS) -> None:
    from orch.host.custody import write_private

    write_private(_relay(state, ws) / "link.json", {"v": 1, "state": "linked"})


# --- the stored level ----------------------------------------------------------------------------------------------


def test_relay_level_is_none_without_a_relay_and_0_without_a_stored_level(tmp_path):
    from orch.store.readerrecs import relay_level, write_relay_level

    state = _state(tmp_path)
    assert relay_level(state, WS) is None
    write_relay_level(state, WS, UID, 1, 5)
    assert relay_level(state, WS) is None  # a level alone is no relay
    _link(state)
    assert relay_level(state, WS) == 1
    (_relay(state) / "reader.json").unlink()
    assert relay_level(state, WS) == 0  # linked, never checked: 0


def test_an_unlinked_relay_counts_with_its_last_level(tmp_path):
    from orch.store.readerrecs import relay_level, write_relay_level

    state = _state(tmp_path)
    _link(state)
    write_relay_level(state, WS, UID, 1, 5)
    (_relay(state) / "link.json").rename(_relay(state) / "unlinked-1.json")
    assert relay_level(state, WS) == 1
    write_relay_level(state, WS, UID, 0, 6)
    assert relay_level(state, WS) == 0


@pytest.mark.parametrize(
    "doc",
    [
        {"v": 1, "reader": 1},
        {"v": 2, "reader": 1, "at_ms": 1},
        {"v": 1, "reader": "1", "at_ms": 1},
        {"v": 1, "reader": True, "at_ms": 1},
        {"v": 1, "reader": -1, "at_ms": 1},
        {"v": 1, "reader": 1, "at_ms": 1, "x": 1},
        [],
    ],
)
def test_a_malformed_stored_level_counts_as_0(tmp_path, doc):
    from orch.host.custody import write_private
    from orch.store.readerrecs import relay_level

    state = _state(tmp_path)
    _link(state)
    write_private(_relay(state) / "reader.json", doc)
    assert relay_level(state, WS) == 0


def test_an_unsafe_stored_level_counts_as_0(tmp_path):
    from orch.store.readerrecs import relay_level, write_relay_level

    state = _state(tmp_path)
    _link(state)
    write_relay_level(state, WS, UID, 1, 5)
    path = _relay(state) / "reader.json"
    os.chmod(path, 0o644)
    assert relay_level(state, WS) == 0
    os.chmod(path, 0o600)
    assert relay_level(state, WS) == 1
    real = path.with_name("real.json")
    path.rename(real)
    path.symlink_to(real)
    assert relay_level(state, WS) == 0


def test_relay_paired_counts_only_a_relay_below_level_1(tmp_path):
    from orch.host import convenience
    from orch.store.readerrecs import write_relay_level

    state = _state(tmp_path)
    _link(state)
    assert convenience._relay_paired(state, WS) is True
    write_relay_level(state, WS, UID, 1, 5)
    assert convenience._relay_paired(state, WS) is False
    write_relay_level(state, WS, UID, 0, 6)
    assert convenience._relay_paired(state, WS) is True


def test_the_relaxed_condition_names_relay_paired_only_below_level_1(tmp_path):
    from orch.ops import readers
    from tests.ops.test_reader_records import Person, _conv, _ws

    me, other = Person(), Person()
    ws = _ws(me, other)
    lv = {d.id: 1 for d in readers.current_devices(ws)}
    readers.check_readers(ws, lv, [_conv(me)], required=1, relay_level=1)
    with pytest.raises(readers.ReadersRefused) as exc:
        readers.check_readers(ws, lv, [_conv(me)], required=1, relay_level=0)
    assert exc.value.reason == "relay_paired"


# --- relay.link: the relaxed rule at link step 1 --------------------------------------------------------------------


def test_a_level_1_relay_is_linked_with_convenience_history_and_its_level_is_stored(tmp_path):
    from orch.host import relaylink
    from orch.store.readerrecs import read_relay_level

    state = _state(tmp_path)
    with relaylink.link_reservation(LinkService(state, ConvStore(CONV)), relay_level=1, clock=lambda: 7.0):
        pass
    assert read_relay_level(state, WS) == 1
    doc = json.loads((_relay(state) / "reader.json").read_text())
    assert doc == {"v": 1, "reader": 1, "at_ms": 7000}
    assert (os.stat(_relay(state) / "reader.json").st_mode & 0o777) == 0o600


def test_a_level_1_relay_is_linked_while_the_flag_is_on(tmp_path):
    from orch.host import convenience, relaylink

    state = _state(tmp_path)
    convenience.set_enabled(state, WS, True, UID)
    with relaylink.link_reservation(LinkService(state, ConvStore()), relay_level=1):
        pass


@pytest.mark.parametrize("level", [0, None])
@pytest.mark.parametrize("why", ["history", "flag", "mark"])
def test_a_relay_below_level_1_is_refused_relay_too_old(tmp_path, level, why):
    from orch.host import convenience, relaylink
    from orch.store.convmark import ensure_convenience_mark

    state = _state(tmp_path)
    store = ConvStore(CONV if why == "history" else [])
    if why == "flag":
        convenience.set_enabled(state, WS, True, UID)
    if why == "mark":
        ensure_convenience_mark(state, WS, UID)
    with pytest.raises(HostError) as exc, relaylink.link_reservation(LinkService(state, store), relay_level=level):
        pytest.fail("reserved")
    assert exc.value.code == "sign.convenience_readers"
    assert exc.value.message.startswith("relay_too_old:")
    assert exc.value.detail == {"reason": "relay_too_old", "devices": []}
    assert not (_relay(state) / "reader.json").exists()


def test_without_convenience_a_level_0_relay_is_linked_and_counts_0(tmp_path):
    from orch.host import convenience, relaylink

    state = _state(tmp_path)
    with relaylink.link_reservation(LinkService(state, ConvStore()), relay_level=0):
        _link(state)
    assert convenience._relay_paired(state, WS) is True


def test_a_refused_relink_never_changes_the_existing_links_level(tmp_path):
    from orch.host import relaylink
    from orch.store.readerrecs import read_relay_level, write_relay_level

    state = _state(tmp_path)
    _link(state)
    write_relay_level(state, WS, UID, 0, 1)
    with relaylink.link_reservation(LinkService(state, ConvStore()), relay_level=1):
        pass  # the store refuses the existing link (relay.link_exists) after the guard
    assert read_relay_level(state, WS) == 0


def test_a_link_without_a_read_level_clears_a_stale_one(tmp_path):
    from orch.host import relaylink
    from orch.store.readerrecs import read_relay_level, write_relay_level

    state = _state(tmp_path)
    write_relay_level(state, WS, UID, 1, 1)  # left from a link that never happened
    with relaylink.link_reservation(LinkService(state, ConvStore()), relay_level=None):
        pass
    assert read_relay_level(state, WS) is None


def test_store_relay_link_with_a_level_1_relay_on_a_marked_workspace(tmp_path):
    from orch.host import relaylink
    from orch.store.convmark import ensure_convenience_mark
    from orch.store.readerrecs import relay_level
    from tests.store.helpers import WS as SWS
    from tests.store.helpers import Env

    env = Env(tmp_path / "w")
    env.bootstrap()
    svc = LinkService(env.host_state, env.store, SWS)
    ensure_convenience_mark(env.host_state, SWS, UID)
    link = {"v": 1, "workspace_id": SWS, "origin": "https://relay.example"}
    with pytest.raises(HostError):
        env.store.relay_link(dict(link), guard=lambda: relaylink.link_reservation(svc, relay_level=0))
    assert not relaylink.link_path(env.host_state, SWS).exists()
    env.store.relay_link(dict(link), guard=lambda: relaylink.link_reservation(svc, relay_level=1))
    assert relay_level(env.host_state, SWS) == 1
    env.store.close()


# --- R1 login: observe the level, relay_reader_old ------------------------------------------------------------------


class Alarms(list):
    def __call__(self, code: str, detail: str) -> None:
        self.append((code, detail))


def test_the_login_hook_stores_the_level_of_a_linked_relay_and_alarms_when_it_drops(tmp_path):
    from orch.host import relaylink
    from orch.store.convmark import ensure_convenience_mark
    from orch.store.readerrecs import read_relay_level

    state = _state(tmp_path)
    alarms = Alarms()
    assert relaylink.observe_relay_level(state, WS, UID, 1, alarms) is False
    assert read_relay_level(state, WS) is None  # no link: nothing stored (an unlinked relay keeps its level)
    _link(state)
    assert relaylink.observe_relay_level(state, WS, UID, 0, alarms) is False  # no convenience: no alarm
    assert read_relay_level(state, WS) == 0 and alarms == []
    ensure_convenience_mark(state, WS, UID)
    assert relaylink.observe_relay_level(state, WS, UID, 1, alarms) is False
    assert relaylink.observe_relay_level(state, WS, UID, 0, alarms) is True
    assert [a[0] for a in alarms] == ["relay_reader_old"]
    assert read_relay_level(state, WS) == 0


def test_the_login_hook_alarms_while_the_flag_is_on(tmp_path):
    from orch.host import convenience, relaylink

    state = _state(tmp_path)
    _link(state)
    convenience.set_enabled(state, WS, True, UID)
    alarms = Alarms()
    assert relaylink.observe_relay_level(state, WS, UID, 0, alarms) is True
    assert [a[0] for a in alarms] == ["relay_reader_old"]


def test_r3_holds_a_restore_with_convenience_evidence_below_level_1():
    from orch.ops.readers import restore_held

    conv, plain = [{"auth": "convenience"}], [{"auth": "passphrase"}]
    assert restore_held(0, conv) is True
    assert restore_held(None, conv) is True
    assert restore_held(1, conv) is False
    assert restore_held(0, plain) is False


# --- relay.pair: no early refusal at the offer -----------------------------------------------------------------------


def test_relay_pair_has_no_early_refusal_with_convenience_history_or_the_flag(tmp_path):
    from orch.host import convenience
    from orch.store.convmark import ensure_convenience_mark

    state = _state(tmp_path)
    convenience.set_enabled(state, WS, True, UID)
    ensure_convenience_mark(state, WS, UID)
    convenience.refuse_reader_op(LinkService(state, ConvStore(CONV)), "relay.pair", history=True)


# --- 11A.15.6 step 4: the host's pair gate (the hook R6c calls) -----------------------------------------------------


class GateService(LinkService):
    def clock(self) -> float:
        from tests.identity.helpers import NOW

        return NOW / 1000


def _gate_args():
    from orch import crypto
    from tests.identity.helpers import Person
    from tests.identity.test_pair_gate import _pair_decl, _phone_meta, _pin

    phone, owner = Person(), Person()
    return phone, owner, _phone_meta, _pair_decl, _pin(owner), crypto


@pytest.mark.parametrize("why", ["history", "flag", "mark"])
def test_the_host_pair_gate_requires_level_1_with_convenience_and_names_the_phone(tmp_path, why):
    from orch.host import convenience
    from orch.store.convmark import ensure_convenience_mark
    from tests.identity.test_pair_gate import OFFER

    state = _state(tmp_path)
    phone, owner, meta_of, decl_of, pin, crypto = _gate_args()
    svc = GateService(state, ConvStore(CONV if why == "history" else []), "705d40abbb8c1c90354a1acaa94c935c")
    if why == "flag":
        convenience.set_enabled(state, svc.workspace_id, True, UID)
    if why == "mark":
        ensure_convenience_mark(state, svc.workspace_id, UID)
    with pytest.raises(HostError) as exc:
        convenience.pair_gate(svc, meta_of(phone), offer_id=OFFER, who=pin)
    assert exc.value.code == "sign.convenience_readers" and exc.value.message.startswith("no_declaration:")
    assert exc.value.detail == {"reason": "no_declaration", "devices": [f"d_{crypto.device_id(phone.sig_pub).hex()}"]}
    assert "too old" in exc.value.hint
    got = convenience.pair_gate(svc, meta_of(phone, decl_of(phone, owner)), offer_id=OFFER, who=pin)
    assert got.level == 1 and got.decl_who == pin


def test_the_host_pair_gate_admits_an_undeclared_phone_without_convenience(tmp_path):
    from orch.host import convenience
    from tests.identity.test_pair_gate import OFFER

    phone, _owner, meta_of, _decl_of, pin, _crypto = _gate_args()
    svc = GateService(_state(tmp_path), ConvStore(), "705d40abbb8c1c90354a1acaa94c935c")
    got = convenience.pair_gate(svc, meta_of(phone), offer_id=OFFER, who=pin)
    assert (got.decl, got.level) == (None, 0)


def test_the_host_pair_gate_refuses_a_bad_declaration_with_reader_decl_invalid(tmp_path):
    from orch.host import convenience
    from tests.identity.test_pair_gate import OFFER

    phone, owner, meta_of, decl_of, pin, _crypto = _gate_args()
    svc = GateService(_state(tmp_path), ConvStore(), "705d40abbb8c1c90354a1acaa94c935c")
    bad = decl_of(phone, owner, bind={"via": "declare"})
    with pytest.raises(HostError) as exc:
        convenience.pair_gate(svc, meta_of(phone, bad), offer_id=OFFER, who=pin)
    assert exc.value.code == "reader.decl_invalid" and exc.value.message.startswith("bind:")
    assert (exc.value.exit, exc.value.retryable) == (3, False)


# --- orch host readers reset (9.10 "A known state restore") ----------------------------------------------------------


def _registered(state: Path, ws: str = WS) -> None:
    from orch.host.custody import write_private
    from orch.host.registry import registry_path

    reg = registry_path(state)
    reg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(reg, {"v": 1, "workspaces": [{"workspace_id": ws, "prefix": "DEMO", "root": "/x"}]})


def test_readers_reset_drops_every_record_and_every_device_counts_0(tmp_path):
    import io

    from orch.host.cli import main
    from orch.host.custody import write_private
    from orch.ops import readers
    from orch.store import readerrecs
    from tests.ops.test_reader_records import WS as RWS
    from tests.ops.test_reader_records import Person, _admit, _ws
    from tests.ops.test_reader_records import _state as model_state

    state = _state(tmp_path)
    _registered(state, RWS)
    a, b = Person(), Person()
    mstate = model_state(_ws(a, b))
    for p in (a, b):
        _admit(state, p)
    write_private(readerrecs.readers_dir(state, RWS) / "x.txt", {"keep": True})  # not a record: never touched
    assert readers.levels(state, RWS, mstate) == {a.device: 1, b.device: 1}
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(state)}
    argv = ["readers", "reset", "--workspace", "DEMO", "--state", str(state)]
    assert main(argv, env={**env, "ORCH_GRANT": "g"}, out=io.StringIO()) != 0
    assert len(readerrecs.device_ids(state, RWS)) == 2  # an agent never resets them
    out = io.StringIO()
    assert main(argv, env=env, out=out) == 0, out.getvalue()
    doc = json.loads(out.getvalue().splitlines()[-1])
    assert doc["ok"] is True and sorted(doc["data"]["dropped"]) == sorted([a.device, b.device])
    assert readerrecs.device_ids(state, RWS) == []
    assert (readerrecs.readers_dir(state, RWS) / "x.txt").exists()
    assert readers.levels(state, RWS, mstate) == {a.device: None, b.device: None}  # fail closed: level 0
    out = io.StringIO()
    assert main(argv, env=env, out=out) == 0
    assert json.loads(out.getvalue().splitlines()[-1])["data"]["dropped"] == []
    assert main(["readers", "reset", "--workspace", "NOPE", "--state", str(state)], env=env, out=io.StringIO()) != 0


def test_readers_reset_refuses_a_linked_readers_directory(tmp_path):
    from orch.store import readerrecs

    state = _state(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    readerrecs.readers_dir(state, WS).parent.mkdir(parents=True, mode=0o700)
    readerrecs.readers_dir(state, WS).symlink_to(real)
    with pytest.raises(OSError):
        readerrecs.reset_records(state, WS)
