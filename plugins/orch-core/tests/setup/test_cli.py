"""B9 step 6: the command: refusals (ORCH_GRANT, no terminal, dev host), dispatch, a full run over the socket."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from orch.host import signer as host_signer
from orch.ops import workspace_init as wi
from orch.setup import flow as flow_mod
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, make_env
from .fakes import FakePorts
from .helpers import call
from .test_secrets import words_for


@pytest.fixture(autouse=True)
def tty(monkeypatch):
    monkeypatch.setattr(wi.TERMINAL, "available", lambda: True)
    monkeypatch.setattr(host_signer, "check", lambda *a, **k: None)
    monkeypatch.setattr(
        host_signer,
        "sign",
        lambda p, *, backend, key_id, workspace_id: host_signer.Signed([{**e, "sig": "x"} for e in p["events"]], None),
    )


def run_main(tmp, argv=(), env=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    ports = kw.pop("ports", None) or FakePorts()
    rc = flow_mod.main(
        list(argv),
        env=env if env is not None else {"USER": "ann"},
        out=out,
        err=err,
        make_env=lambda e: make_env(tmp, ports=lambda: ports, clock=lambda: ports.now),
        ensure_host=kw.pop("ensure_host", lambda e, err: None),
        run_dir=kw.pop("run_dir", tmp.root / "run"),
        cwd=kw.pop("cwd", str(tmp.work)),
        **kw,
    )
    return rc, out.getvalue(), err.getvalue()


def test_usage_and_dispatch(tmp):
    rc, _, err = run_main(tmp, ["extra"])
    assert rc == 2 and "usage: orch setup" in err
    from orch.cli.main import main as cli_main

    e = io.StringIO()
    assert cli_main(["setup", "extra"], env={"HOME": str(tmp.home)}, stdout=io.StringIO(), stderr=e) == 2
    assert "usage: orch setup" in e.getvalue()


@pytest.mark.parametrize("env", [{"ORCH_GRANT": "gr_x"}, {"ORCH_GRANT": ""}, {"ORCH_SESSION": "s_x"}])
def test_agents_are_refused_before_anything_starts(tmp, env):
    started = []
    rc, _, err = run_main(tmp, env=env, ensure_host=lambda e, err: started.append(1))
    assert rc == 3 and "an agent never does it" in err and started == []
    assert not (tmp.root / "run").exists()


def test_no_terminal_is_refused(tmp, monkeypatch):
    monkeypatch.setattr(wi.TERMINAL, "available", lambda: False)
    rc, _, err = run_main(tmp)
    assert rc == 3 and "terminal" in err


def test_a_dev_host_is_refused(tmp):
    def dev(env, err):
        raise SetupError("host.unavailable", "the orch host could not be started (dev checkout)", 500)

    rc, _, err = run_main(tmp, ensure_host=dev)
    assert rc == 1 and "dev checkout" in err


def test_a_refused_directory_stops_before_a_browser_opens(tmp):
    opened = []
    rc, _, err = run_main(tmp, cwd=str(tmp.home), open_browser=lambda e, er, launch: opened.append(1) or True)
    assert rc == 1 and "home directory" in err and opened == []


def test_an_existing_project_dir_is_fine(tmp):
    (tmp.work / "README.md").write_text("hi")
    seen = {}

    def browser(env, err, launch):
        seen.update(launch)
        call(str(tmp.root / "run" / "setup.sock"), "POST", "/shutdown", {})
        return True

    rc, _, err = run_main(tmp, open_browser=browser)
    assert seen["code"] and rc == 1  # shut down without finishing: not a success


def test_the_whole_command_over_the_socket(tmp):
    ports = FakePorts()
    sock = str(tmp.root / "run" / "setup.sock")
    result = {}

    def browser(env, err, launch):
        def drive():
            st, c, _ = call(sock, "POST", "/claim", {"code": launch["code"]})
            tok = c["token"]
            api = lambda m, r, b=None: call(sock, m, "/api/setup/" + r, b, token=tok)  # noqa: E731
            assert api("POST", "profile", {**PROFILE, "addons": ["dashboard", "quick"]})[0] == 200
            st, rev, _ = api("POST", "secrets/reveal", {})
            assert api("POST", "secrets/confirm", {"words": words_for(rev)})[1] == {"ok": True}
            assert api("POST", "secrets/reveal", {})[0] in (409, 410)
            st, rv, h = api("POST", "review", {})
            ports.expect(wait_flow().review.plan)
            st, created, _ = api("POST", "create", {"review_id": rv["review_id"], "digest": rv["digest"]})
            assert st == 200
            for _ in range(100):
                st, p, _ = api("GET", f"progress?run_id={created['run_id']}")
                if p["done"] or p["error"]:
                    break
                threading.Event().wait(0.05)
            result["progress"] = p
            st, fin, _ = api("POST", "finish", {})
            result["finish"] = (st, fin)
            call(sock, "POST", "/shutdown", {})  # never leave the command waiting, whatever happened above

        def wait_flow():
            return holder["flow"]

        def safe():
            try:
                drive()
            except BaseException as e:  # noqa: BLE001 - a failed assertion must end the command, not hang it
                result["error"] = repr(e)
                call(sock, "POST", "/shutdown", {})

        threading.Thread(target=safe, daemon=True).start()
        return True

    holder = {}
    real = flow_mod.SetupFlow

    class Capturing(real):
        def __init__(self, env):
            super().__init__(env)
            holder["flow"] = self

    flow_mod.SetupFlow = Capturing  # type: ignore[misc]
    try:
        rc, out, err = run_main(tmp, ports=ports, open_browser=browser)
    finally:
        flow_mod.SetupFlow = real  # type: ignore[misc]
    assert "error" not in result, result
    assert rc == 0, (err, result)
    assert "browser connected" in err and "set up in" in out
    assert result["progress"]["done"] is True and result["finish"][0] == 200
    assert ports.passphrase_seen and ports.passphrase_seen not in out + err and ports.code_seen not in out + err
    assert not Path(sock).exists()  # the socket is gone with the process


def test_ctrl_c_before_the_run_starts_stops_between_steps(tmp):
    ports = FakePorts()
    f = SetupFlow(make_env(tmp, ports=lambda: ports, clock=lambda: ports.now))
    f.launch(str(tmp.work))
    f.dispatch("POST", "profile", {}, {**PROFILE, "addons": ["dashboard"]})
    from .test_secrets import reveal

    r = reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"words": words_for(r)})
    rv = f.dispatch("POST", "review", {}, {})[1]
    ports.expect(f.review.plan)
    f.cancel_run()
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = f.dispatch("GET", "progress", {"run_id": out["run_id"]}, None)[1]
    assert p["error"]["code"] == "stop" and p["error"]["finishable"] is False and ports.log == []
