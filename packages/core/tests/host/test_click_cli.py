"""H5c CLI: ``orch host convenience on|off|show`` (the per-workspace flag) and ``orch keys`` (custody transitions)."""

from __future__ import annotations

import io
import json
import os

from orch.cli.main import main
from orch.custody import PassphraseBackend
from orch.custody import convenience as conv
from orch.custody.convenience import FakeStore
from orch.host import convenience
from orch.ops import human
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.ops.humans import KDF, Provider

lab, served, human_fixture = human_helpers.lab, human_helpers.served, human_helpers.human


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=env or {"HOME": "/nonexistent"}, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_the_flag_is_switched_per_workspace_by_the_host_cli(served, monkeypatch):
    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)
    from orch.custody import select

    monkeypatch.setattr(select, "status", lambda st, wid: {"custody": "convenience", "device": "d", "gen": 1})
    state = str(served.lab.state)
    assert not convenience.enabled(served.lab.state, served.wid, UID)
    code, out, _ = run("host", "convenience", "on", "--workspace", served.service.entry.prefix, "--state", state)
    assert code == 0 and "agents running as you, or anything that takes over this dashboard" in out
    assert convenience.enabled(served.lab.state, served.wid, UID)
    assert (served.lab.state / "host" / "convenience.json").stat().st_mode & 0o777 == 0o600
    code, out, _ = run("host", "convenience", "off", "--workspace", served.wid, "--state", state)
    assert code == 0 and not convenience.enabled(served.lab.state, served.wid, UID)
    code, out, _ = run("host", "convenience", "on", "--workspace", "NOPE", "--state", state)
    assert code == 2
    code, out, _ = run(
        "host", "convenience", "on", "--workspace", served.wid, "--state", state, env={"ORCH_GRANT": "x"}
    )
    assert code == 3 and "human_only" in out


def test_host_convenience_on_moves_a_passphrase_key_into_click_custody(served, monkeypatch):
    """Run 9 feedback 3.7: `on` used to set the flag only and leave "on, not held". Now one command ends consistent."""
    from orch.custody import cli as keys_cli
    from orch.custody import exposure, select

    state = served.lab.state
    provider = Provider()
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    human.open_backend(human.keys_dir(state, served.wid)).create("dk", role="device", passphrase=provider.passphrase)
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: None)
    monkeypatch.setattr(convenience, "_own_device", lambda st, wid: served.lab.ws.owner.device)
    base = ("host", "convenience", "on", "--workspace", served.wid, "--state", str(state))
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: False)
    code, out, _ = run(*base)
    assert code != 0 and "terminal" in out and not convenience.enabled(state, served.wid, UID)
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: True)
    code, out, _ = run(*base)
    assert code == 0, out
    assert convenience.enabled(state, served.wid, UID) and select.status(state, served.wid)["custody"] == "convenience"
    assert "reader level" not in out  # no foreign reader: no reader warning


def test_convenience_defaults_to_the_workspace_of_the_current_folder(served, monkeypatch):
    """F3: inside a workspace folder `--workspace` is that folder's workspace; elsewhere the error names the fix."""
    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)
    state = str(served.lab.state)
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    monkeypatch.chdir(served.service.entry.root)
    monkeypatch.setenv("PWD", os.getcwd())
    from orch.custody import select as _select

    real_status = _select.status  # B8: `on` infers the folder's workspace exactly like `off`
    monkeypatch.setattr(_select, "status", lambda st, wid: {**real_status(st, wid), "custody": "convenience"})
    code, out, _ = run("host", "convenience", "on", "--state", state)
    monkeypatch.setattr(_select, "status", real_status)
    assert code == 0 and "this folder belongs to" in out and served.service.entry.prefix in out
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert code == 0 and not convenience.enabled(served.lab.state, served.wid, UID)
    assert "this folder belongs to" in out and served.service.entry.prefix in out
    assert "one-click custody" not in out and "orch sign" not in out  # no key held here: neither claim is made
    from orch.custody import select

    real = select.status
    monkeypatch.setattr(select, "status", lambda st, wid: {**real(st, wid), "custody": "passphrase"})
    code, out, _ = run("host", "convenience", "off", "--state", state)
    # a key already in passphrase custody signs in the Terminal: no "stays in one-click custody" claim
    assert "Sign in the terminal with the action’s CLI command" in out and "stays in one-click custody" not in out
    monkeypatch.setattr(select, "status", lambda st, wid: {**real(st, wid), "custody": "convenience"})
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert (
        code == 0 and "stays in one-click custody" in out and "orch keys custody passphrase" in out
    )  # F1: the way to a passphrase
    assert "Sign in the terminal with the action’s CLI command" not in out
    monkeypatch.setattr(select, "status", real)
    monkeypatch.chdir("/")
    monkeypatch.setenv("PWD", "/")
    code, out, _ = run("host", "convenience", "off", "--state", state)
    assert code == 5 and "invalid.input" in out
    assert "describe describe" not in out and "convenience off --workspace <your workspace prefix>" in out
    code, out, _ = run("host", "convenience", "on", "--state", state)
    assert code == 5 and "convenience on --workspace <your workspace prefix>" in out


def test_orch_keys_converts_only_with_the_flag_and_moves_back_only_in_place(served, monkeypatch):
    state = served.lab.state
    provider = Provider()
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    pub = human.open_backend(human.keys_dir(state, served.wid)).create(
        "dk", role="device", passphrase=provider.passphrase
    )
    store = FakeStore()
    monkeypatch.setattr(conv, "open_store", lambda s: store)
    from orch.custody import exposure

    marked = []  # #1097 E3: the signed exposure mark comes first (tests: test_key_exposure_custody)
    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: marked.append(a[2]))
    from orch.custody import cli as keys_cli

    monkeypatch.setattr(keys_cli, "has_terminal", lambda: False)
    base0 = ("--workspace", served.wid, "--state", str(state), "--json")
    code, out, _ = run("keys", "custody", "convenience", *base0)
    err0 = json.loads(out)["error"]
    assert code == 3 and err0["code"] == "custody.no_prompt" and "retryable" in err0 and "hint" in err0
    monkeypatch.setattr(keys_cli, "has_terminal", lambda: True)
    base = ("--workspace", served.wid, "--state", str(state), "--json")
    code, out, _ = run("keys", "custody", "convenience", *base)
    assert code == 3 and json.loads(out)["error"]["code"] == "sign.convenience_unavailable"
    code, out, _ = run("keys", "custody", "convenience", *base, env={"ORCH_GRANT": "x"})
    assert json.loads(out)["error"]["code"] == "human_only"
    convenience.set_enabled(state, served.wid, True, UID)
    code, out, err = run("keys", "custody", "convenience", *base)
    assert code == 0, out
    assert "could sign" in err
    res = json.loads(out)["result"]
    assert res["custody"] == "convenience" and marked == [res["device"]]
    code, out, _ = run("keys", "custody", "status", "--state", str(state), "--json")
    (row,) = json.loads(out)["result"]["workspaces"]
    assert row["custody"] == "convenience" and row["enabled"] is True
    code, out, _ = run("keys", "custody", "passphrase", *base)
    assert code == 5 and "--in-place" in json.loads(out)["error"]["message"]
    code, out, _ = run("keys", "custody", "passphrase", "--rotate", *base)
    assert "one-click" in json.loads(out)["error"]["message"]  # #1097: --rotate needs click-to-sign off first
    monkeypatch.setattr(PassphraseBackend, "create", _with_passphrase(PassphraseBackend.create, provider.passphrase))
    code, out, err = run("keys", "custody", "passphrase", "--in-place", *base)
    assert code == 0, out
    assert "--rotate" in err and json.loads(out)["result"]["custody"] == "passphrase"
    assert human.open_backend(human.keys_dir(state, served.wid)).public_key("dk") == pub
    code, out, _ = run("keys", "forget", *base)
    assert code == 0


def _with_passphrase(create, passphrase):
    def wrapped(self, key_id, *, secret=None, role="device", passphrase_=None, **kw):
        kw.pop("passphrase", None)
        return create(self, key_id, secret=secret, role=role, passphrase=passphrase)

    return wrapped


def test_the_on_hint_gives_the_placeholder_when_the_folder_is_reached_through_a_symlink(served, monkeypatch, tmp_path):
    """A copyable hint must not be steerable: a planted symlink ($PWD != real cwd) gets the placeholder."""
    state = str(served.lab.state)
    link = tmp_path / "link"
    link.symlink_to(served.service.entry.root)
    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))  # the logical path differs from the physical one
    code, out, _ = run("host", "convenience", "on", "--state", state)
    assert code == 5 and "convenience on --workspace <your workspace prefix>" in out
    assert served.service.entry.prefix not in out


def test_convenience_off_asks_to_move_the_key_back_only_with_a_terminal(served, monkeypatch):
    """V3-D31: `off` clears the flag under the lock (select.turn_off_convenience), then, with a terminal and a key in
    convenience custody, moves the key back WITHOUT holding the lock during the prompt (L2-1075-4)."""
    from orch.custody import cli as custody_cli
    from orch.custody import select

    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)
    state = str(served.lab.state)
    calls, moves = [], []
    monkeypatch.setattr(select, "turn_off_convenience", lambda st, wid, uid, **kw: calls.append(kw))
    monkeypatch.setattr(select, "status", lambda st, wid: {"custody": "convenience", "device": "d", "gen": 1})
    monkeypatch.setattr(select, "downgrade_in_place", lambda st, wid, **kw: moves.append(wid) or select.Selection({}))
    monkeypatch.setattr(custody_cli, "has_terminal", lambda: False)
    code, out, _ = run("host", "convenience", "off", "--workspace", served.wid, "--state", state)
    assert code == 0 and calls == [{"move_key": False}] and moves == [] and "back under your passphrase" not in out
    monkeypatch.setattr(custody_cli, "has_terminal", lambda: True)
    code, out, _ = run("host", "convenience", "off", "--workspace", served.wid, "--state", state)
    assert code == 0 and moves == [served.wid] and "back under your passphrase" in out
    monkeypatch.setattr(select, "downgrade_in_place", lambda *a, **k: (_ for _ in ()).throw(select.CustodyError("x")))
    code, out, _ = run("host", "convenience", "off", "--workspace", served.wid, "--state", state)
    assert code != 0 and "could not be moved back" in out and "orch keys custody passphrase" in out


def test_passphrase_prompt_wording_for_an_existing_key_moving_back():
    """B9: moving a key back under a passphrase is not "create this key"."""
    from orch.custody.passphrase import PassphraseRequest, render_prompt

    assert "action: create this key" in render_prompt(PassphraseRequest("create", "dk", "", ""))
    moved = render_prompt(PassphraseRequest("create", "dk", "", "", existing=True))
    assert "protect your key with a passphrase again" in moved and "create this key" not in moved
