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

The flow: preview (no grant: `launch.no_grant`), issue the runner grant by click against core's contract-4 signer
(deposit confirmed, the secret only at the runner), preview again, start a `codex` agent, see it in the run list, forget
the grant; the page's live stream carries run.sub's `runner` item after the confirm and after the forget (H11c-2). A
second grant, forgotten between its issue and its hand-over, is a real rejected deposit with `deposit_reason`
`grant.forgotten`; a deposit whose answer is lost and whose runner restarts resolves from unknown to rejected through
`grant.deposit_status` (the host's `runner_restarted` reads `other`). The contract-1 path ("update orch") is a unit test
with a fake signer (test_dashboard_click).
"""

import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
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


class LiveTap:
    """The page's live stream (`Live.stream`, the SSE body a tab reads) on a thread of its own: every frame it sends."""

    def __init__(self, aw):
        from orch_dashboard.live import Live

        self.frames, self.queue = [], queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.task = None
        ready = threading.Event()

        async def read():
            live = Live(aw.gateway, aw.auth)
            w, tab = live.attach(aw.w.wid, aw.token)
            stream = live.stream(w, tab)
            try:
                async for frame in stream:
                    self.queue.put(frame)
                    if "event: ready" in frame:
                        ready.set()
            finally:
                await stream.aclose()
                await live.close()

        def main():
            asyncio.set_event_loop(self.loop)
            self.task = self.loop.create_task(read())
            try:
                self.loop.run_until_complete(self.task)
            except BaseException:  # noqa: BLE001 - cancelled at close
                pass

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        assert ready.wait(10), "the live stream never became ready"

    def runner_item(self, match, timeout=10.0):
        """The next `runs` event's `runner` item whose grant `match`es (earlier frames are kept in `frames`)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.queue.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.frames.append(frame)
            if "event: runs" not in frame:
                continue
            for item in json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0]):
                if item.get("t") == "runner" and match(item.get("grant")):
                    return item
        raise AssertionError(f"no matching runner item on the live stream: {self.frames}")

    def close(self):
        if self.task is not None and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(10)
        while not self.queue.empty():
            self.frames.append(self.queue.get_nowait())


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
        self.taps = []
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
            self.rstate = rstate
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
            self.client = TestClient(
                create_app(PORT, self.gateway, self.auth, static),
                base_url=ORIGIN,
                headers={"X-Orch-Click-Contract": "4"},
            )
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

    def tap(self):
        tap = LiveTap(self)
        self.taps.append(tap)
        return tap

    def close(self):
        for tap in self.taps:
            tap.close()
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
    """Prepare, view, step the dwell, sign with one click: the dashboard's click routes, core's signer."""
    p = aw.post("/click/prepare", {"action": "runner_grant", "hours": hours, "scope": scope})
    assert p.status_code == 200, p.text
    rid = p.json()["request_id"]
    v = aw.post("/click/view", {"request_id": rid})
    assert v.status_code == 200, v.text
    view = v.json()["view"]
    aw.click.mono.t += 1.5
    s = aw.post("/click/sign", {"request_id": rid, "view_digest": v.json()["view_digest"]})
    assert s.status_code == 200, s.text
    return rid, view, s


def test_preview_grant_by_click_start_an_agent_and_forget(aw, caplog, capfd):
    key = aw.w.ticket()[1]
    args = {"ticket": key, "harness": "codex", "mode": "work"}
    pre = aw.post("/agents/preview", args)
    assert pre.status_code == 200, pre.text
    assert pre.json()["blocked"]["code"] == "launch.no_grant" and pre.json()["blocked"]["detail"] == {"reason": "none"}
    assert aw.get("/runner").json()["runner"]["enabled"] is True
    assert aw.get("/runner").json()["runner"]["grant"] is None

    assert core_contract() == 4  # the real deposit against the real contract-4 signer, no gate set aside
    tap = aw.tap()  # the page's live stream, opened before anything changes (run.sub does not replay the grant)

    rid, view, signed = issue_grant(aw)
    assert view["deposit"] is True and view["confirm"] == {"kind": "click"}
    from orch.click.protocol import DEPOSIT_RISK

    # run 13 C3: core's own risk lines of agent access, with the length, and a headline, through this server unchanged
    assert view["risk_lines"] == [DEPOSIT_RISK]
    assert isinstance(view["headline"], str) and view["headline"]
    body = signed.json()
    assert body["deposit"] == "confirmed" and body["deposit_id"] == rid[3:] and body["grant"].startswith("gr_")
    assert set(body) <= {"state", "duplicate", "deposit", "deposit_id", "grant", "deposit_reason"}
    assert "deposit_reason" not in body  # only with a rejected deposit
    gid = body["grant"]
    secret = aw.runner.grants.secret_for(aw.w.wid, aw.w.lab.ws.owner.ref, gid)
    tail = secret.partition(".")[2]
    assert tail and tail not in signed.text and tail not in json.dumps(view)
    card = aw.get("/runner").json()["runner"]
    assert card["grant"]["id"] == gid and tail not in json.dumps(card)
    assert aw.get(f"/runner/deposits/{rid[3:]}").json() == {"deposit": "confirmed"}
    confirmed = tap.runner_item(lambda g: g is not None and g["id"] == gid)  # run.sub's runner item reached the page
    assert set(confirmed["grant"]) == {"id", "until", "scope"} and confirmed["grant"]["until"] == card["grant"]["until"]

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
    tap.runner_item(lambda g: g is None)  # and the forget reached it too
    assert aw.post("/agents/preview", args).json()["blocked"]["code"] == "launch.no_grant"
    aw.post(f"/runs/{run['run']}/stop")
    tap.close()
    assert tail not in json.dumps(tap.frames)
    out, errs = capfd.readouterr()
    assert tail not in caplog.text and tail not in out and tail not in errs  # never in a log of this process


def test_a_grant_forgotten_before_its_hand_over_is_a_rejected_deposit_with_its_reason(aw):
    """A real rejected deposit: the grant is issued, then forgotten (the dashboard's own forget, a real host op) before
    the signer hands it to the runner. The host refuses the deposit with `grant.forgotten`; the sign answer says
    `rejected`, names the grant (Revoke uses it) and the static reason, and never the secret."""
    from orch.host.control_client import new_idem

    assert core_contract() == 4
    forgotten = []

    def between(op, args, res):  # timing only: the host's answer is passed on unchanged
        issued = re.search(r'"grant": "(gr_[0-7][0-9A-HJKMNP-TV-Z]{25})"', json.dumps(res))
        if op == "human.submit" and issued and not forgotten:
            link = aw.gateway.enabled(aw.w.wid)
            forgotten.append((issued[1], link.call("grant.forget", {"grant": issued[1]}, write=True, idem=new_idem())))
        return res

    aw.click.tamper = between
    rid, _view, signed = issue_grant(aw)
    aw.click.tamper = None
    assert forgotten and forgotten[0][1] == {"forgotten": False}  # issued but never held: nothing to drop
    body = signed.json()
    assert body["deposit"] == "rejected" and body["deposit_reason"] == "grant.forgotten"
    assert body["grant"] == forgotten[0][0] and body["deposit_id"] == rid[3:]
    assert aw.get("/runner").json()["runner"]["grant"] is None
    # The host refused it before recording a deposit: grant.deposit_status knows none.
    assert aw.get(f"/runner/deposits/{rid[3:]}").json() == {"deposit": "none"}


def test_an_unknown_deposit_resolves_to_rejected_with_the_reason_from_deposit_status(aw, monkeypatch):
    """The runner takes the grant but its answer is lost (unknown), then the runner restarts without it: the host
    settles the deposit as rejected (`runner_restarted`), and the dashboard's deposit-status route says `rejected` with
    `reason` mapped to the closed set (`other`)."""
    from orch.runner.config import parse

    from tests.host.test_runner_socket import GEN2, Runner

    assert core_contract() == 4
    aw.w.h.host.runners.command_timeout = 1.0
    aw.runner.link.drop_reports.add("grant.put")
    rid, _view, signed = issue_grant(aw)
    did = rid[3:]
    assert signed.json()["deposit"] == "unknown" and "deposit_reason" not in signed.json()
    assert aw.get(f"/runner/deposits/{did}").json() == {"deposit": "unknown"}
    doc = json.loads((Path(aw.w.lab.state) / "runner.json").read_text())
    aw.runner.close()
    aw.runner = Runner(aw.w, aw.rstate, gen=GEN2, config=parse(doc))  # restarted: it does not hold the grant
    deadline = time.monotonic() + 10
    while (got := aw.get(f"/runner/deposits/{did}").json())["deposit"] != "rejected" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert got == {"deposit": "rejected", "reason": "other"}  # the host's `runner_restarted` is outside the closed set
