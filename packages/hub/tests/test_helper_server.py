"""HUB-SV-02 piece c: the helper's rules on real keys and stores, and its socket."""

import json
import os
import socket
import threading

import pytest
from channels_helpers import Env, register

from orch_hub.contract.canon import b64u
from orch_hub.services import helper_backends as B
from orch_hub.services import helper_proto as P
from orch_hub.services import helper_server as HS
from orch_hub.services.helper_verify import ReadOnlyAuth

CFG = B.parse_config(
    {
        "v": 1,
        "host_id": "vps1",
        "parts": {
            "hub": {"label": "Hub", "kind": "systemd", "unit": "orch-hub.service"},
            "relay": {"label": "Relay", "kind": "systemd", "unit": "orch-relay.service"},
        },
    }
)


class Rig:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.state = tmp_path / "hub"
        self.admin = self.env.user("admin")
        self.env.acc.add_member("hub-admin", self.admin)
        self.signer, cert = register(self.env, self.admin)
        self.chan = cert["o"]["channel_id"]
        self.ran: list[list[str]] = []
        self.rc = 0
        self.deferred: list = []
        self.nonces = P.NonceStore(self.state / "helper-nonce.db", now_ms=self.env.clock)
        self.svc = HS.HelperService(
            state=self.state,
            config=CFG,
            auth=ReadOnlyAuth(self.state, now_ms=self.env.clock),
            nonces=self.nonces,
            runner=self._run,
            now_ms=self.env.clock,
            defer=lambda d, fn: self.deferred.append(fn),
            logs=lambda part: f"log of {part.name}",
        )

    def _run(self, argv, timeout):
        self.ran.append(list(argv))
        return self.rc, "", "boom" if self.rc else ""

    def prepare(self, part="relay", action="restart", user=None, chan=None):
        return self.svc.handle(
            {
                "op": "prepare",
                "user_id": user or self.admin,
                "channel_id": chan or self.chan,
                "part": part,
                "action": action,
            }
        )

    def run(self, p, proof=None, user=None, chan=None, nonce=None):
        proof = proof if proof is not None else b64u(self.signer.proof(bytes.fromhex(p["digest"])))
        return self.svc.handle(
            {
                "op": "run",
                "user_id": user or self.admin,
                "channel_id": chan or self.chan,
                "nonce": nonce or p["ctx"]["nonce"],
                "proof": proof,
            }
        )

    def close(self):
        self.nonces.close()
        self.env.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_signed_restart_runs_the_fixed_argv_once(rig):
    p = rig.prepare()
    assert p["ok"], p
    assert p["ctx"]["kind"] == P.TAG and p["ctx"]["part"] == "relay" and p["text"].startswith("Restart Relay")
    assert p["ctx"]["hub_id"] == rig.env.keys.hub_id and p["digest"] == P.digest(p["ctx"]).hex()
    r = rig.run(p)
    assert r == {"ok": True, "accepted": True, "will_restart": False}
    assert rig.ran == [["sudo", "-n", "/usr/bin/systemctl", "restart", "orch-relay.service"]]
    again = rig.run(p)  # replay
    assert again["code"] == "unknown_prepared" and len(rig.ran) == 1


def test_wrong_proof_burns_the_nonce_and_runs_nothing(rig):
    p = rig.prepare()
    assert rig.run(p, proof=b64u(b"\x01" * 64))["code"] == "bad_channel_proof"
    assert rig.run(p)["code"] == "unknown_prepared" and rig.ran == []


def test_proof_over_another_digest_is_refused(rig):
    p, q = rig.prepare(action="stop"), rig.prepare(action="restart")
    # q replaced p for the same person; sign p's digest and present it for q's nonce
    assert rig.run(q, proof=b64u(rig.signer.proof(bytes.fromhex(p["digest"]))))["code"] in ("bad_channel_proof",)
    assert rig.ran == []


def test_expired_prepare(rig):
    p = rig.prepare()
    rig.env.clock.advance(P.TTL_MS + 1)
    assert rig.run(p)["code"] == "prepared_expired" and rig.ran == []


def test_not_an_admin_and_revoked_channel_fail_closed(rig):
    p = rig.prepare()
    ann = rig.env.user("ann")
    _, c2 = register(rig.env, ann)
    ch2 = c2["o"]["channel_id"]
    assert rig.prepare(user=ann, chan=ch2)["code"] == "forbidden_role"
    rig.env.acc.revoke_channel(rig.chan)
    assert rig.prepare()["code"] == "channel_revoked"
    assert rig.run(p)["code"] in ("forbidden_role", "channel_revoked", "bad_channel_proof", "unavailable")
    assert rig.ran == []


def test_part_action_and_update_are_closed(rig):
    for bad in ("../x", "sshd", "Relay", None, 5):
        assert rig.prepare(part=bad)["code"] == "unknown_part"
    for bad in ("status", "edit", "restart;x", None):
        assert rig.prepare(action=bad)["code"] == "bad_action"
    assert rig.prepare(action="update")["code"] == "update_unavailable"
    assert rig.svc.handle({"op": "prepare", "user_id": "x", "channel_id": rig.chan})["code"] == "malformed"
    assert rig.svc.handle({"op": "nope"})["code"] == "unknown_op"
    assert rig.svc.handle("x")["code"] == "malformed"


def test_unreadable_store_fails_closed(rig, tmp_path):
    os.rename(rig.state / "access.db", rig.state / "access.moved")
    assert rig.prepare()["code"] == "unavailable"


def test_hub_restart_answers_first_and_blocks_others(rig):
    p = rig.prepare(part="hub", action="restart")
    assert "console goes offline" in p["text"]
    assert rig.run(p) == {"ok": True, "accepted": True, "will_restart": True}
    assert rig.ran == []  # not yet: deferred
    q = rig.prepare()
    assert rig.run(q)["code"] == "busy" and rig.ran == []  # one global lock while the hub restarts
    rig.deferred[0]()  # the deferred command runs, and the lock goes
    assert rig.ran == [["sudo", "-n", "/usr/bin/systemctl", "restart", "orch-hub.service"]]
    q2 = rig.prepare()
    assert rig.run(q2)["ok"] is True


def test_failing_command_is_reported_short(rig):
    rig.rc = 1
    p = rig.prepare()
    r = rig.run(p)
    assert r["code"] == "action_failed" and r["detail"] == "boom"


def test_per_part_lock_is_busy(rig):
    p = rig.prepare()
    assert rig.svc._locks["relay"].acquire(blocking=False)
    assert rig.run(p)["code"] == "busy"
    rig.svc._locks["relay"].release()


def test_every_run_is_audited_before_it_happens(rig):
    p = rig.prepare()
    rig.run(p)
    lines = [json.loads(x) for x in (rig.state / HS.AUDIT_FILE).read_text().splitlines()]
    assert lines[-1]["op"] == "run" and lines[-1]["part"] == "relay" and lines[-1]["user"] == rig.admin
    assert oct((rig.state / HS.AUDIT_FILE).stat().st_mode & 0o777) == "0o600"


# --- the socket -----------------------------------------------------------------------------------------------------


def _ask(path, obj):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(path))
    s.sendall(json.dumps(obj).encode() + b"\n")
    out = b""
    while chunk := s.recv(4096):
        out += chunk
    s.close()
    return json.loads(out) if out else None


@pytest.fixture
def short(tmp_path):  # a unix socket path must stay under ~100 bytes
    import shutil
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp(prefix="hs", dir="/tmp"))
    yield d
    shutil.rmtree(d)


def test_socket_is_private_serves_our_uid_and_refuses_another(rig, short):
    tmp_path = short
    path = tmp_path / "s.sock"
    srv = HS.HelperServer(rig.svc, path)
    srv.bind()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert _ask(path, {"op": "parts"})["parts"][0]["name"] == "hub"
        assert _ask(path, "garbage")["code"] == "malformed"
    finally:
        srv.stop()
    other = HS.HelperServer(rig.svc, tmp_path / "o.sock", uid=os.geteuid() + 1)  # a peer with another uid
    other.bind()
    t2 = threading.Thread(target=other.serve_forever, daemon=True)
    t2.start()
    try:
        assert _ask(tmp_path / "o.sock", {"op": "parts"}) is None  # closed without a word
    finally:
        other.stop()


def test_bind_refuses_a_path_that_is_not_our_socket(rig, short):
    f = short / "x.sock"
    f.write_text("not a socket")
    with pytest.raises(P.HelperError):
        HS.HelperServer(rig.svc, f).bind()
    assert f.read_text() == "not a socket"


def test_bind_refuses_a_too_long_path(rig, tmp_path):
    with pytest.raises(P.HelperError):
        HS.HelperServer(rig.svc, tmp_path / ("d" * 120) / "s.sock").bind()


def test_cli_prints_sudoers_and_refuses_without_config(tmp_path, capsys):
    import json

    from orch_hub import cli

    os.chmod(tmp_path, 0o700)
    assert cli.main(["helper", "--state", str(tmp_path), "--print-sudoers", "--user", "orch-hub"]) == 1
    f = tmp_path / "helper.json"
    f.write_text(
        json.dumps(
            {
                "v": 1,
                "host_id": "vps1",
                "parts": {"relay": {"label": "Relay", "kind": "systemd", "unit": "orch-relay.service"}},
            }
        )
    )
    os.chmod(f, 0o600)
    assert cli.main(["helper", "--state", str(tmp_path), "--print-sudoers", "--user", "orch-hub"]) == 0
    assert "NOPASSWD: ORCH_SVC" in capsys.readouterr().out


def test_logs_are_admin_only_audited_and_part_closed(rig):
    ok = rig.svc.handle({"op": "logs", "user_id": rig.admin, "channel_id": rig.chan, "part": "relay"})
    assert ok == {"ok": True, "part": "relay", "text": "log of relay"}
    last = json.loads((rig.state / HS.AUDIT_FILE).read_text().splitlines()[-1])
    assert last["op"] == "logs" and last["user"] == rig.admin and last["part"] == "relay"
    ann = rig.env.user("ann")
    _, c2 = register(rig.env, ann)
    bad = rig.svc.handle({"op": "logs", "user_id": ann, "channel_id": c2["o"]["channel_id"], "part": "relay"})
    assert bad["code"] == "forbidden_role"
    assert (
        rig.svc.handle({"op": "logs", "user_id": rig.admin, "channel_id": rig.chan, "part": "../x"})["code"]
        == "unknown_part"
    )
    n = len((rig.state / HS.AUDIT_FILE).read_text().splitlines())
    os.chmod(rig.state / HS.AUDIT_FILE, 0o400)  # an audit line that cannot be written refuses the read
    r = rig.svc.handle({"op": "logs", "user_id": rig.admin, "channel_id": rig.chan, "part": "relay"})
    assert r["code"] == "unavailable" and len((rig.state / HS.AUDIT_FILE).read_text().splitlines()) == n


def test_client_over_the_real_socket(rig, short):
    from orch_hub.services.helper_client import HelperClient

    path = short / "c.sock"
    cl = HelperClient(path)
    assert cl.info() == {"state": "not_set_up", "parts": []}  # no socket file yet
    srv = HS.HelperServer(rig.svc, path)
    srv.bind()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert cl.info()["state"] == "up" and cl.info()["host_id"] == "vps1"
        with pytest.raises(P.HelperError) as e:
            cl.call("prepare", user_id=rig.admin, channel_id=rig.chan, part="../x", action="stop")
        assert e.value.code == "unknown_part"
    finally:
        srv.stop()
    assert cl.info()["state"] == "not_set_up"  # the socket file is gone with the server


def test_bind_refuses_when_a_helper_already_answers_but_replaces_a_stale_socket(rig, short):
    path = short / "d.sock"
    a = HS.HelperServer(rig.svc, path)
    a.bind()
    threading.Thread(target=a.serve_forever, daemon=True).start()
    try:
        with pytest.raises(P.HelperError) as e:
            HS.HelperServer(rig.svc, path).bind()
        assert e.value.code == "already_running"
    finally:
        a._stop.set()
        a._sock.close()  # dead server, socket file stays: stale
    HS.HelperServer(rig.svc, path).bind()


def test_failed_command_output_is_redacted(rig):
    rig.rc = 1
    rig.ran.clear()
    orig = rig._run
    rig.svc._run = lambda argv, t: (1, "", "failed token=abc12345678")
    r = rig.run(rig.prepare())
    assert r["code"] == "action_failed" and "abc12345678" not in r["detail"]
