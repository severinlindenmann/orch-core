"""R2 T3 (#1590): ``orch remote on|off|level|status|device|key``: who may run it, what is signed and what is not, and
the terminal flow of adding a device (code, the device's six words, the backup key). The host and the signer are
replaced;
``orch remote enable|disable|reset`` (the HTTPS front) is untouched and still reached."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import remote, remote_access
from orch.ops.errors import OrchError

WORDS = ["abandon", "ability", "able", "about", "above", "absent"]
PENDING = "rp_" + "1" * 32
DEVICE = "rd_" + "a" * 32


def run(argv, env=None, *, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    code = remote.main([*argv, *(["--json"] if as_json else [])], env=env or {}, out=out, err=err)
    return code, out.getvalue()


def never(*a, **k):
    raise AssertionError("the host must not be reached")


@pytest.fixture
def offline(monkeypatch):
    for name in ("_target", "_call", "_sign"):
        monkeypatch.setattr(remote_access, name, never)


@pytest.fixture
def host(monkeypatch):
    """A host that records what the CLI signs and calls, and the order of both."""
    log = SimpleNamespace(signed=[], called=[], order=[], reply={}, tty=True, pending=[[]], status={})
    monkeypatch.setattr(remote_access, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(remote_access, "_tty_ok", lambda: log.tty)

    def sign(t, op, args):
        log.order.append(("sign", op))
        log.signed.append((op, args))

    def call(t, op, args, env):
        log.order.append(("call", op))
        log.called.append((op, args))
        if op == "remote.pending":
            return {"pending": log.pending.pop(0) if len(log.pending) > 1 else log.pending[0]}
        reply = log.reply.get(op, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(remote_access, "_sign", sign)
    monkeypatch.setattr(remote_access, "_call", call)
    monkeypatch.setattr(remote_access, "_sleep", lambda s: None)
    return log


@pytest.mark.parametrize(
    "argv",
    [["on", "--level", "work", "--premises", "no"], ["level", "view"], ["off"], ["status"], ["device", "add"],
     ["device", "list"], ["device", "remove", DEVICE], ["key", "rotate"]],
)  # fmt: skip
@pytest.mark.parametrize("env", [{"ORCH_GRANT": "gr_x"}, {"ORCH_SESSION": "s.1"}])
def test_an_agent_calling_any_remote_verb_is_refused_before_anything(offline, argv, env):
    code, out = run(argv, env)
    assert code == 3 and out.startswith("error op.not_allowed")


def test_the_refusal_in_json(offline):
    code, out = run(["off"], {"ORCH_GRANT": "gr_x"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed"


@pytest.mark.parametrize(
    "argv",
    [["on"], ["on", "--level", "work"], ["on", "--level", "weird", "--premises", "no"],
     ["on", "--level", "work", "--premises", "no", "--days", "0"], ["on", "--level", "work", "--premises", "no", "x"],
     ["level"], ["level", "root"], ["level", "view", "--premises", "yes"], ["off", "now"],
     ["status", "--level", "view"],
     ["device"], ["device", "add", "--premises", "no"], ["device", "remove"], ["device", "remove", "x"],
     ["key"], ["key", "x"], ["device", "list", "--agents-can-act-as-me"]],
)  # fmt: skip
def test_a_malformed_command_prints_the_usage_and_touches_nothing(offline, argv):
    code, out = run(argv)
    assert code in (2, 5) and ("usage: orch remote" in out or "error invalid.input" in out)


def test_off_works_without_an_owner_proof_or_a_terminal(host):
    host.tty = False
    host.reply["remote.off"] = {"changed": True}
    code, out = run(["off"])
    assert code == 0 and "ok remote access is off" in out
    assert host.called == [("remote.off", {})] and host.signed == []
    host.reply["remote.off"] = {"changed": False}
    assert "already off" in run(["off"])[1]


def test_on_signs_remote_set_with_the_owners_answers(host):
    code, out = run(["on", "--level", "work", "--premises", "no", "--days", "3", "--agents-can-act-as-me"])
    assert code == 0 and "backup key" in out and "orch remote device add" in out
    assert host.signed == [("remote.set", {"level": "work", "premises": "no", "days": 3, "agents_risk": True})]
    assert host.called == []


def test_on_without_a_terminal_signs_nothing(host):
    host.tty = False
    code, out = run(["on", "--level", "view", "--premises", "no"])
    assert code == 3 and "no terminal" in out and host.signed == []


def test_a_host_refusal_of_the_policy_is_plain(host, monkeypatch):
    def sign(t, op, args):
        raise remote_access.PubError("remote.agents_risk", "agents on this computer can act as you", 3)

    monkeypatch.setattr(remote_access, "_sign", sign)
    code, out = run(["on", "--level", "view", "--premises", "no"])
    assert code == 3 and "error remote.agents_risk" in out


def test_level_keeps_the_premises_answer_and_needs_remote_on(host):
    host.reply["remote.status"] = {"on": False, "premises": None}
    code, out = run(["level", "view"])
    assert code == 5 and "turn it on first" in out and host.signed == []
    host.reply["remote.status"] = {"on": True, "premises": "yes", "level": "work"}
    code, _ = run(["level", "terminals", "--days", "2"])
    assert code == 0 and host.signed == [("remote.set", {"level": "terminals", "premises": "yes", "days": 2})]


def test_status_lists_the_devices_with_their_six_words(host):
    host.reply["remote.status"] = {
        "on": True, "level": "work", "until_ms": 1_700_000_000_000, "premises": "no", "codes": 1,
        "devices": [{"device_id": DEVICE, "name": "Laptop", "level_cap": "work", "expires_ms": 1_700_000_000_000,
                     "words": WORDS}],
    }  # fmt: skip
    code, out = run(["status"])
    assert code == 0 and "Remote access is on (work), until 2023-11-14 22:13 UTC" in out
    assert DEVICE in out and " ".join(WORDS) in out and "1 code(s) waiting" in out
    d = json.loads(run(["status"], as_json=True)[1])
    assert d["ok"] is True and d["data"]["devices"][0]["device_id"] == DEVICE
    host.reply["remote.status"] = {"on": False, "off_reason": "hub_full", "devices": []}
    out = run(["status"])[1]
    assert "off (the hub reads this workspace" in out and "no devices" in out


def test_device_list_and_remove_and_key_rotate_need_no_signature(host):
    host.tty = False
    host.reply["remote.status"] = {"devices": []}
    assert "no devices" in run(["device", "list"])[1]
    host.reply["remote.device_remove"] = {"device_id": DEVICE, "removed": True}
    assert run(["device", "remove", DEVICE])[0] == 0
    host.reply["remote.key_rotate"] = {"wrk_fp": "00", "removed": [DEVICE]}
    code, out = run(["key", "rotate"])
    assert code == 0 and "added again" in out
    assert [c[0] for c in host.called] == ["remote.status", "remote.device_remove", "remote.key_rotate"]
    assert host.called[1][1] == {"device_id": DEVICE} and host.signed == []


def test_cli_device_add_terminal_flow(host):
    """The code is shown once, the CLI waits for the device, shows its name and six words, then the owner's backup key
    (the signer's own prompt) adds it: code -> pending -> words -> confirm, in that order."""
    host.reply["remote.code"] = {
        "code": "orch-rc1.WS.AAAA.BBBB",
        "code_id": "rc_" + "2" * 32,
        "expires_ms": 4_000_000_000_000,
    }
    host.pending = [[], [], [{"pending": PENDING, "name": "Ann's phone", "words": WORDS, "presence_kind": "platform",
                              "expires_ms": 1}]]  # fmt: skip
    code, out = run(["device", "add", "--level", "view", "--days", "90"])
    assert code == 0, out
    assert out.count("orch-rc1.WS.AAAA.BBBB") == 1  # shown once
    assert f"Add device Ann's phone, fingerprint {' '.join(WORDS)}?" in out and "backup key" in out
    assert host.order == [("call", "remote.code"), ("call", "remote.pending"), ("call", "remote.pending"),
                          ("call", "remote.pending"), ("sign", "remote.device_confirm")]  # fmt: skip
    assert host.signed == [("remote.device_confirm", {"pending": PENDING, "level_cap": "view", "days": 90})]
    assert out.index("orch-rc1") < out.index("Add device") < out.index("signed: the device is added")


def test_device_add_defaults_to_work_for_30_days_never_terminals(host):
    host.reply["remote.code"] = {"code": "c", "expires_ms": 4_000_000_000_000}
    host.pending = [[{"pending": PENDING, "name": "x", "words": WORDS}]]
    assert run(["device", "add"])[0] == 0
    assert host.signed == [("remote.device_confirm", {"pending": PENDING, "level_cap": "work", "days": 30})]


def test_device_add_gives_up_when_nobody_uses_the_code(host, monkeypatch):
    host.reply["remote.code"] = {"code": "c", "expires_ms": 4_000_000_000_000}
    clock = iter(range(0, 10**6, 400))
    monkeypatch.setattr(remote_access, "_now", lambda: next(clock))
    code, out = run(["device", "add"])
    assert code == 3 and "error remote.no_pending" in out and host.signed == []


def test_device_add_without_a_terminal_makes_no_code(host):
    host.tty = False
    code, out = run(["device", "add"])
    assert code == 3 and "no terminal" in out and host.called == []


def test_a_full_code_list_is_the_hosts_plain_refusal(host):
    host.reply["remote.code"] = OrchError("remote.full", "four enrollment codes are already waiting")
    code, out = run(["device", "add"])
    assert code == 3 and out.startswith("error remote.full")


def test_the_https_front_commands_are_still_reached(monkeypatch):
    """enable|disable|reset stay in orch.cli.remote; only the new verbs go to remote_access."""
    monkeypatch.setattr(remote_access, "main", never)
    code, out = run(["enable"])  # missing --name: its own parser answers, not ours
    assert code != 0
    seen = []
    monkeypatch.setattr(remote_access, "main", lambda argv, **kw: seen.append(list(argv)) or 0)
    assert run(["status"])[0] == 0 and seen == [["status"]]


def test_sessions_lists_by_number_and_end_uses_the_number_not_the_raw_id(host):
    host.tty = False
    rows = [
        {"stream_id": "a" * 32, "device_id": DEVICE, "name": "Severin's MacBook", "person": "p_" + "1" * 32,
         "level": "work", "since_ms": 1_800_000_000_000},
        {"stream_id": "b" * 32, "device_id": DEVICE, "name": None, "person": None, "level": None, "since_ms": None},
    ]  # fmt: skip
    host.reply["remote.sessions"] = {"sessions": rows}
    code, out = run(["sessions"])
    assert code == 0 and "1  Severin's MacBook  work" in out and "2  (connecting)" in out
    assert "a" * 32 not in out and DEVICE not in out  # no raw ids on screen
    code, out = run(["end", "1"])
    assert code == 0 and "session 1 ended" in out
    assert host.called[-1] == ("remote.session_end", {"stream_id": "a" * 32}) and host.signed == []
    code, out = run(["end", "3"])
    assert code == 3 and "remote.no_stream" in out
    assert run(["end", "x"])[0] == 2 and run(["end"])[0] == 2
    assert run(["sessions"], as_json=True)[1].startswith('{"ok": true')


@pytest.mark.parametrize("argv", [["sessions"], ["end", "1"]])
def test_an_agent_cannot_list_or_end_sessions(offline, argv):
    code, out = run(argv, {"ORCH_GRANT": "gr_x"})
    assert code != 0 and "op.not_allowed" in out
