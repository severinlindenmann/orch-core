"""dev-pair's real-orch steps against a real sandbox host (no relay): ``orch setup`` through the setup socket and the
pseudo-terminal (generated passphrase, recovery code confirmed), the D2 sandbox files, ``orch host run --foreground
--dev-sim-convenience``, and ``orch keys reader declare``; then ``down``. The relay steps
(account, link, pair) are the lead's end-to-end run after the pairing branch merges."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from orch import devsandbox

pytestmark = pytest.mark.slow


@pytest.fixture
def base():
    """A short private directory below the real home (the host refuses run directories directly below a sticky ``/tmp``;
    pytest's ``HOME`` is a ``/tmp`` directory, so the account's own home is asked), removed by its exact path."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    d = Path(tempfile.mkdtemp(prefix=".dpt", dir=home))
    os.chmod(d, 0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def make_ctx(dp, base, *, sim):
    root = dp.check_root_path(str(base / "sb"))
    sb = dp.create_root(root)
    for n in dp.TOP_ENTRIES:
        sb.mkdir(n, dp.RUN_MODE if n == "run" else 0o700)
    dp.manifest_update(root, entries=[*dp.TOP_ENTRIES, "pair.json", "primary-cert.json"])
    path_env, py, uv = dp.pinned_path()
    args = Namespace(sim_convenience=sim, relay="local", scopes="look,decide", json_fd=None, open_simulator=False,
                     invite_file=None, expect_code_from=None)  # fmt: skip
    ctx = dp.Ctx(sb=sb, args=args, path_env=path_env, py=py, uv=uv)
    ctx.ws_dir, ctx.origin = str(root / "ws"), "http://127.0.0.1:1"
    return ctx


@pytest.mark.parametrize("sim", [True, False], ids=["sim-convenience", "plain"])
def test_setup_host_and_down(dp, base, sim, capsys):
    """V3-D38: the host first (a PENDING dev host under ``--sim-convenience``), ``orch setup`` through the setup
    socket and the pseudo-terminal, then for a sandbox: stop, bind person, workspace row and marker, start the full
    dev host."""
    dash = Path(__file__).resolve().parents[4] / "addons" / "dashboard" / "server" / ".venv" / "bin" / "orch-dashboard"
    if not dash.is_file():
        pytest.skip("orch setup needs the dashboard add-on (uv sync --locked in addons/dashboard/server)")
    ctx = make_ctx(dp, base, sim=sim)
    root = ctx.root
    try:
        prefix, name = ("SBX", devsandbox.owner_name(ctx.sb.sid)) if sim else ("DVP", f"dev-pair {ctx.sb.sid[:8]}")
        if sim:
            dp.step_sim_pre(ctx, ctx.sb.sid)
            dp.step_host(ctx, role="host-pending")
            assert "pending genesis, no dev powers" in (root / "logs" / "host-pending.log").read_text()
        else:
            dp.step_host(ctx)
        dp.step_setup(ctx, prefix, name)
        dp.identity_from_disk(ctx)
        # the secrets: 0600, the recovery code is the one the genesis owner is derived from
        for f in ("passphrase", "recovery-code"):
            assert (os.stat(root / "secrets" / f).st_mode & 0o777) == 0o600
        assert len(ctx.sb.read("secrets/recovery-code").decode().split()) == 24
        if sim:
            dp.stop_host(ctx, "host-pending")
            dp.step_sim(ctx, ctx.sb.sid)
            m = devsandbox.check_manifest(root)
            assert m["person_id"] == ctx.person_id and m["workspaces"][0]["id"] == ctx.workspace_id
            dev = json.loads((root / "state" / "host" / "dev.json").read_text())
            assert (
                dev["sim_convenience"] is True and dev["relay_origin"] == ctx.origin and dev["sandbox_id"] == ctx.sb.sid
            )
            assert devsandbox.read_marker(root / "ws")["workspace_id"] == ctx.workspace_id
            dp.step_host(ctx)
            host_log = (root / "logs" / "host.log").read_text()
            assert "DEV HOST: Simulator convenience answers ON" in host_log
        else:
            dp.manifest_update(root, person_id=ctx.person_id)
            assert not (root / "state" / "host" / "dev.json").exists()
            host_log = (root / "logs" / "host.log").read_text()
            assert "DEV HOST" not in host_log
        procs = devsandbox.check_manifest(root)["processes"]
        assert [p["role"] for p in procs] == ["host"] and dp.validate_record(procs[0], os.path.realpath(root))
        assert dp.host_listening(root)
        if sim:
            dp.pty_cmd(ctx, ctx.orch("keys", "reader", "declare", "--workspace", ctx.workspace_id), "reader declare")
            # ask: a grant, a ticket and a free-text question as the agent; nobody has answered yet
            capsys.readouterr()
            rc = dp.main(["ask", "--root", str(root), "--nonce", "ab12cd34"])
            assert rc == 0, capsys.readouterr().err
            made = json.loads(capsys.readouterr().out)
            assert made["question"] == "Q1" and made["ticket"].startswith("SBX-") and "GRANT" not in json.dumps(made)
            assert dp.main(["check-answer", "--root", str(root), "--nonce", "ab12cd34", "--timeout", "0"]) == 1
            captured = capsys.readouterr()  # no phone has paired in this sandbox: refused, nothing is scanned
            assert "refused" in captured.err and "answered" not in captured.out
        # no secret in the logs
        for log in (root / "logs").iterdir():
            text = log.read_text()
            assert ctx.passphrase not in text and ctx.sb.read("secrets/recovery-code").decode() not in text
    finally:
        dp.down(str(root), grace=10, out=lambda s: None)
        ctx.sb.close()
    assert not root.exists()
    assert not procs or dp.proc_identity(procs[0]["pid"]) is None
