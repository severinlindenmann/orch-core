"""M7b Start agent end to end: the dashboard server against core's host (in this process, like the M7a terminals), a
real runner, and core's real click signer.

- **The harness** is core's own seam (`tests/host/test_agents.py`): `runner.json` with `login_shell:false` names a fake
  `codex`/`claude` program in tmp_path that records its argv and environment and sleeps. No real agent ever starts.
- **The signer** is core's `ClickSigner` (the code `python -I -m orch.click` runs) in this process, built by core's test
  helper `tests.host.test_click.Click` with the owner's device key in core's `FakeStore` (the custody test seam) and its
  monotonic clock (the 1 s dwell is stepped, not slept). It reaches the dashboard through the dashboard's own `Signer`
  and its `start` seam, so every frame goes through `click_signer.py` and `click.py` as in production. Not the
  `click_child.py` subprocess: that child needs a host it can reach through the person's registry with a file-store
  key, and the runner can only be admitted by a host in this process (`runner_identity`, see test_realhost_terminals).
- Everything lives under pytest's tmp_path and core's short run directory: no launchd, no keychain, no ~/.config/orch.

The flow: preview (no grant: `launch.no_grant`), issue the runner grant by click (deposit confirmed, the secret only at
the runner), preview again, start a `codex` agent, see it in the run list, forget the grant.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from termkit import ORIGIN, PORT, grant, grant_dashboard

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only"),
]


class Chan:
    """The dashboard Signer's child, in this process: core's ClickSigner behind the channel interface."""

    def __init__(self, click):
        self.click, self.proc = click, None

    def call(self, op, args, timeout=None):
        return self.click.signer.handle(op, json.loads(json.dumps(args)))

    def close(self):
        pass


class AgentWorld:
    def __init__(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        from orch.runner import enablement
        from orch.runner.config import parse

        from orch_dashboard.app import create_app
        from orch_dashboard.auth import Auth
        from orch_dashboard.click_signer import Signer
        from orch_dashboard.gateway import Gateway
        from tests.host.lifecycle_helpers import UID, Lab
        from tests.host.live_helpers import World, ok
        from tests.host.test_agents import fake
        from tests.host.test_click import Click
        from tests.host.test_runner_socket import Runner

        monkeypatch.setenv("SHELL", "/bin/sh")
        monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)

        class Solo(World):
            """core's live World without its second member: click-to-sign is refused while another member reads."""

            def __init__(self, lab, **kw):
                self.quinn, self.lab = None, lab
                self.h = lab.host(clock=lambda: lab.ws.clock[0], **kw)
                self.h.__enter__()
                self.control = self.h.client()
                self.wid = ok(lab.register(self.control, peers=lab.peers(lab.owner)))["data"]["workspace_id"]
                self.service = self.h.host.services[self.wid]
                self.bus = self.h.host.bus
                self.clients = []

        lab = Lab(tmp_path)
        try:
            self.w = w = Solo(lab, runner_identity=lambda cred: cred.pid == os.getpid())
        except BaseException:
            lab.close()
            raise
        self.runner = self.click = self.client = self.gateway = None
        try:
            grant_dashboard(w)
            grant(w, "start-agent", ["spawn_agent"])
            enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=False, text="t")
            programs = {"codex": str(fake(tmp_path, "codex")), "claude-code": str(fake(tmp_path, "claude"))}
            doc = {"v": 1, "login_shell": False, "programs": programs}
            cfg = Path(w.lab.state) / "runner.json"
            cfg.write_text(json.dumps(doc))
            cfg.chmod(0o600)
            rstate = tmp_path / "rs"
            rstate.mkdir(mode=0o700)
            self.runner = Runner(w, rstate, config=parse(doc))
            self.out = tmp_path / "out"
            self.click = Click(w)
            monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(w.lab.run))
            monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
            self.gateway = Gateway(
                Path(w.lab.state), click=Signer(Path(w.lab.state), start=lambda env: Chan(self.click))
            )
            self.auth = Auth()
            static = tmp_path / "static"
            (static / "assets").mkdir(parents=True)
            (static / "index.html").write_text("<html><head></head></html>")
            self.client = TestClient(create_app(PORT, self.gateway, self.auth, static), base_url=ORIGIN)
            self.client.__enter__()
            res = self.client.post(
                "/auth/session", json={"code": self.auth.launch(PORT)["code"]}, headers={"Origin": ORIGIN}
            )
            self.token = res.json()["token"]
        except BaseException:
            self.close()
            raise

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Origin": ORIGIN, "Content-Type": "application/json"}

    def get(self, path):
        return self.client.get(f"/api/w/{self.w.wid}{path}", headers=self.headers())

    def post(self, path, body=None):
        return self.client.post(f"/api/w/{self.w.wid}{path}", content=json.dumps(body or {}), headers=self.headers())

    def word(self):
        return f"{self.w.service.entry.prefix}~{self.w.wid[-8:]}/GRANT"

    def close(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
        if self.gateway is not None:
            self.gateway.close()
        if self.click is not None:
            self.click.close()
        if self.runner is not None:
            self.runner.close()
        self.w.h.stop()
        self.w.lab.close()


@pytest.fixture
def aw(tmp_path, monkeypatch):
    import types

    from orch_dashboard.adapters import handoff as handoff_adapter

    try:
        world = AgentWorld(tmp_path, monkeypatch)
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        pytest.skip(f"no unix sockets here: {exc}")
    # core's test host runs on its own clock (the Lab's): a request's `expires_at` is checked against that clock here.
    monkeypatch.setattr(handoff_adapter, "time", types.SimpleNamespace(time=lambda: world.w.lab.ws.clock[0]))
    yield world
    world.close()


def core_contract() -> int:
    from orch.click import protocol

    return protocol.CONTRACT


def issue_grant(aw, hours=8, scope="workable"):
    """Prepare, view, step the dwell, sign with the typed word: the dashboard's click routes, core's signer."""
    p = aw.post("/click/prepare", {"action": "runner_grant", "hours": hours, "scope": scope})
    assert p.status_code == 200, p.text
    rid = p.json()["request_id"]
    v = aw.post("/click/view", {"request_id": rid})
    assert v.status_code == 200, v.text
    view = v.json()["view"]
    aw.click.mono.t += 1.5
    s = aw.post("/click/sign", {"request_id": rid, "view_digest": v.json()["view_digest"], "word": aw.word()})
    assert s.status_code == 200, s.text
    return rid, view, s


def test_preview_grant_by_click_start_an_agent_and_forget(aw, monkeypatch, caplog, capfd):
    from orch_dashboard import click_signer

    key = aw.w.ticket()[1]
    args = {"ticket": key, "harness": "codex", "mode": "work"}
    pre = aw.post("/agents/preview", args)
    assert pre.status_code == 200, pre.text
    assert pre.json()["blocked"]["code"] == "launch.no_grant" and pre.json()["blocked"]["detail"] == {"reason": "none"}
    assert aw.get("/runner").json()["runner"]["enabled"] is True
    assert aw.get("/runner").json()["runner"]["grant"] is None

    contract = core_contract()
    if contract < 2:
        # This core's signer is contract 1: the dashboard keeps the runner grant terminal only (update orch).
        res = aw.post("/click/prepare", {"action": "runner_grant"})
        assert res.status_code == 409 and res.json()["error"]["why"] == "update_orch"
        # #466's signer already hands a grant to the runner; set the contract gate aside to exercise the real deposit.
        monkeypatch.setattr(click_signer.Signer, "deposits", lambda self: True)
    else:
        monkeypatch.setattr(click_signer, "CONTRACT", contract, raising=False)

    rid, view, signed = issue_grant(aw)
    assert view["deposit"] is True and view["confirm"]["word"] == aw.word()
    body = signed.json()
    assert body["deposit"] == "confirmed" and body["deposit_id"] == rid[3:] and body["grant"].startswith("gr_")
    assert set(body) <= {"state", "duplicate", "deposit", "deposit_id", "grant", "deposit_reason"}
    gid = body["grant"]
    secret = aw.runner.grants.secret_for(aw.w.wid, aw.w.lab.ws.owner.ref, gid)
    tail = secret.partition(".")[2]
    assert tail and tail not in signed.text and tail not in json.dumps(view)
    card = aw.get("/runner").json()["runner"]
    assert card["grant"]["id"] == gid and tail not in json.dumps(card)
    assert aw.get(f"/runner/deposits/{rid[3:]}").json() == {"deposit": "confirmed"}

    pre = aw.post("/agents/preview", args)
    assert pre.json()["blocked"] is None and pre.json()["grant"]["id"] == gid
    assert "ORCH_GRANT" in pre.json()["env"] and tail not in pre.text

    from orch.host.control_client import new_idem

    idem = new_idem()
    start = aw.post("/agents/start", {**args, "spec": pre.json()["spec"], "idem": idem})
    assert start.status_code == 200, start.text
    run = start.json()["run"]
    assert run["kind"] == "agent" and run["run"] == "r_" + idem[2:] and run["grant_state"] == "ok"
    assert "session" not in run and "grant" not in run
    again = aw.post("/agents/start", {**args, "spec": pre.json()["spec"], "idem": idem})  # a retry: the same run
    assert again.status_code == 200 and again.json()["run"]["run"] == run["run"]
    listed = {r["run"]: r for r in aw.get("/runs").json()["runs"]}
    assert listed[run["run"]]["kind"] == "agent" and listed[run["run"]]["harness"] == "codex"

    forgot = aw.post("/runner/forget", {"grant": gid, "idem": new_idem()})
    assert forgot.status_code == 200 and forgot.json() == {"forgotten": True}
    assert aw.get("/runner").json()["runner"]["grant"] is None
    assert aw.post("/agents/preview", args).json()["blocked"]["code"] == "launch.no_grant"
    aw.post(f"/runs/{run['run']}/stop")
    out, errs = capfd.readouterr()
    assert tail not in caplog.text and tail not in out and tail not in errs  # never in a log of this process
