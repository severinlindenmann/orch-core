"""``orch-hub spaces`` on a state dir, persistence across processes, private files and ``inspect``."""

from __future__ import annotations

import json
import os
import stat

import pytest

from orch_hub import cli
from orch_hub.access import Access
from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.inspect import inspect
from orch_hub.keys import HubKeys
from orch_hub.link import Registry

ADMIN = G.fake16("cli_admin")


def join(reg, ws, wsk, mode="full"):
    pk = G.K.pub["pk_alice"]
    b = H.verify_binding(
        G.binding(ws=ws, wsk=wsk, mode=mode), workspace_id=ws, wsk_pub=G.K.pub[wsk], now_ms=G.NOW, dev_mode=True
    )
    reg.commit_join(
        ws,
        H.person_id(pk),
        G.K.b(wsk),
        G.b64u(pk),
        G.v2_card(ws=ws, wsk=wsk, wxk_pub=G.K.b("wxk_a" if ws == G.WS_A else "wxk_b")),
        H.apply_binding(None, b),
    )


@pytest.fixture
def state(tmp_path):
    st = tmp_path / "hub"
    keys = HubKeys.init(st)
    reg = Registry(str(st / "registry.db"))
    join(reg, G.WS_A, "wsk_a")
    join(reg, G.WS_B, "wsk_b", "selective")
    reg.close()
    acc = Access(str(st / "access.db"))
    acc.add_user(ADMIN, "admin")
    acc.add_member("hub-admin", ADMIN)
    acc.close()
    return st, keys


def run(st, capsys, *argv, code=0):
    rc = cli.main(["spaces", "--state", str(st), *argv])
    out = capsys.readouterr()
    assert rc == code, out.err
    return json.loads(out.out if code == 0 else out.err)


def test_create_list_add_remove(state, capsys):
    st, _ = state
    c = run(st, capsys, "create", "--name", "finance", "--workspace", G.WS_A, "--as", ADMIN)
    assert c["epoch"] == 1 and c["members"] == [G.WS_A]
    sid = c["space_id"]
    (row,) = run(st, capsys, "list")
    assert row["name"] == "finance" and row["epoch"] == 1 and row["members"] == [G.WS_A]
    need = run(st, capsys, "add", "--space", sid, "--workspace", G.WS_B, "--as", ADMIN, code=1)
    assert need["error"] == "not_accepted" and need["need"]["workspaces"] == [G.WS_B]  # B is selective
    assert run(st, capsys, "remove", "--space", sid, "--workspace", G.WS_A, "--as", ADMIN) == {"deleted": True}
    assert run(st, capsys, "list") == []


def test_the_actor_must_be_allowed(state, capsys):
    st, _ = state
    err = run(st, capsys, "create", "--name", "x", "--workspace", G.WS_A, "--as", G.U_BOB, code=1)
    assert err == {"error": "forbidden_role"}


def test_output_has_no_key_material(state, capsys):
    st, _ = state
    run(st, capsys, "create", "--name", "finance", "--workspace", G.WS_A, "--as", ADMIN)
    with open(st / "spaces.db", "rb") as f:
        raw = f.read()
    sk = __import__("sqlite3").connect(str(st / "spaces.db")).execute("SELECT sk FROM space_epoch").fetchone()[0]
    assert sk in raw  # the hub holds SK_e (option B) ...
    out = json.dumps(inspect(st))
    assert sk.hex() not in out and G.b64u(sk) not in out  # ... and never prints it


def test_spaces_db_is_private(state, capsys):
    st, _ = state
    run(st, capsys, "create", "--name", "finance", "--workspace", G.WS_A, "--as", ADMIN)
    m = stat.S_IMODE(os.stat(st / "spaces.db").st_mode)
    assert m == 0o600


def test_inspect_shows_spaces(state, capsys):
    st, _ = state
    sid = run(st, capsys, "create", "--name", "finance", "--workspace", G.WS_A, "--as", ADMIN)["space_id"]
    (sp,) = inspect(st)["spaces"]
    assert sp["space_id"] == sid and sp["members"] == [G.WS_A] and sp["epochs"][0]["epoch"] == 1
    assert sp["epochs"][0]["sk_commit"] and sp["items"] == []
