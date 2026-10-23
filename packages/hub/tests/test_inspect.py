"""``orch-hub inspect``: read-only, dev-only registry view with op counters (S8 invariants)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from orch_hub.cli import main
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import unb64u
from orch_hub.inspect import InspectError, inspect
from orch_hub.keys import HubKeys
from orch_hub.link import LinkError, Registry


def joined(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    return c


def expect_link_refusal(c, msg, code):
    c.send_raw(msg)
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == code


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "hub"
    HubKeys.init(s)
    return s


def run_cli(capsys, *argv):
    rc = main(["inspect", *argv])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_selective_join_then_leave(make_hub, state, capsys):
    hub = make_hub(registry=Registry(str(state / "registry.db")))
    c = joined(hub)
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    rc, out, _ = run_cli(capsys, "--state", str(state), "--json")
    assert rc == 0
    d = json.loads(out)
    assert d["dev"] is True and d["hub_id"]
    w = d["workspaces"][G.WS_A]
    assert w["workspace_id"] == G.WS_A and w["owner_person_id"] == G.P_ALICE
    assert w["wsk_pub"] == G.K.b("wsk_a")
    assert w["wsk_fingerprint"] == hashlib.sha256(unb64u(G.K.b("wsk_a"), 65)).hexdigest()
    assert w["mode"] == "selective"
    assert w["binding"] == {"state": "unbound", "binding_seq": 1}
    assert w["ops"]["wk_seal"] == {"accepted": 0, "refused": 0}
    assert w["ops"]["join"] == {"accepted": 1, "refused": 0}
    assert w["ops"]["binding_put"]["accepted"] == 1
    # workspace filter
    rc, out, _ = run_cli(capsys, "--state", str(state), "--workspace", "f" * 32, "--json")
    assert rc == 0 and json.loads(out)["workspaces"] == {}


def test_refused_wk_seal_is_counted_separately(make_hub, state):
    hub = make_hub(registry=Registry(str(state / "registry.db")))
    c = joined(hub)
    expect_link_refusal(c, c.build("wk_seal", {"epoch": 1, "wrap": "AA"}), "wrong_mode")
    w = inspect(state)["workspaces"][G.WS_A]
    assert w["ops"]["wk_seal"] == {"accepted": 0, "refused": 1}


def test_refused_handler_op_is_counted(make_hub, state):
    hub = make_hub(registry=Registry(str(state / "registry.db")))
    c = joined(hub)
    with pytest.raises(LinkError):
        c.request("binding_put", {"object": G.binding(seq=1, mode="full")})  # same seq, different binding
    w = inspect(state)["workspaces"][G.WS_A]
    assert w["ops"]["binding_put"]["refused"] == 1


def test_read_only_while_server_holds_db(make_hub, state):
    reg_path = state / "registry.db"
    hub = make_hub(registry=Registry(str(reg_path)))
    joined(hub)
    before = reg_path.read_bytes()
    inspect(state)
    inspect(state)
    assert reg_path.read_bytes() == before
    # the inspect connection itself cannot write
    db = sqlite3.connect(f"{reg_path.resolve().as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO op_counts(ws, op, outcome, count) VALUES ('x','wk_seal','accepted',1)")
    db.close()
    # and the live server still works after inspecting
    assert hub.registry.pin(G.WS_A) is not None


def test_output_has_only_public_fields(make_hub, state):
    hub = make_hub(registry=Registry(str(state / "registry.db")))
    joined(hub)
    d = inspect(state)
    assert set(d) == {"dev", "hub_id", "workspaces", "unjoined_ops", "spaces", "audit"}
    assert set(d["workspaces"][G.WS_A]) == {
        "workspace_id", "owner_person_id", "wsk_pub", "wsk_fingerprint", "mode", "binding", "ops", "snapshot", "routes", "wk_epochs", "shares",
    }  # fmt: skip


def test_non_dev_card_refused(state, capsys):
    Registry(str(state / "registry.db")).close()
    meta = json.loads((state / "meta.json").read_text())
    p = state / "op" / str(meta["op_seq"]) / "card.json"
    card = json.loads(p.read_text())
    card["o"]["dev"] = False
    p.write_text(json.dumps(card))
    with pytest.raises(InspectError):
        inspect(state)
    rc, out, err = run_cli(capsys, "--state", str(state), "--json")
    assert rc == 1 and out == "" and "dev" in err


def test_old_registry_without_op_log(state):
    db = sqlite3.connect(state / "registry.db")
    db.execute(
        "CREATE TABLE workspace (workspace_id TEXT PRIMARY KEY, owner_person_id TEXT NOT NULL, wsk_pub TEXT NOT NULL,"
        " pk_pub TEXT NOT NULL, card TEXT NOT NULL, binding_state TEXT NOT NULL, binding_obj TEXT)"
    )
    db.commit()
    db.close()
    assert inspect(state)["workspaces"] == {}


def test_unjoined_link_does_not_grow_the_table(make_hub, state):
    hub = make_hub(registry=Registry(str(state / "registry.db")))
    db = sqlite3.connect(state / "registry.db")

    def rows():
        return db.execute("SELECT COUNT(*) FROM op_counts").fetchone()[0]

    def refused_once():  # an unjoined link ends after its first non-join op (D12b G8): one link per attempt
        c = hub.open(first=True)
        with pytest.raises(LinkError):
            c.request("snapshot_get", {})
        c.close()

    for _ in range(3):
        refused_once()
    base = rows()
    for _ in range(100):
        refused_once()
    assert rows() == base
    d = inspect(state)
    assert d["workspaces"] == {} and d["unjoined_ops"]["snapshot_get"]["refused"] == 103
    db.close()


def test_inspect_shows_share_counts_only(make_hub, state):
    from orch_hub.shares import ShareService, ShareStore

    store = ShareStore(str(state / "shares.db"))
    reg = Registry(str(state / "registry.db"))
    hub = make_hub(registry=reg, shares=ShareService(store, reg, hub_id=G.HUB_ID, hxk=G.K.priv["hxk"], dev_mode=True))
    c = joined(hub)
    from orch_hub.contract import vectorgen_p4 as V4

    b = V4.share_body(V4.share_item(item_id=G.fake16("i1")))
    c.request("share_put", b)
    w = inspect(state)["workspaces"][G.WS_A]
    assert w["shares"] == {"items": 1, "bytes": len(unb64u(b["sealed"]))}
    assert w["ops"]["share_put"] == {"accepted": 1, "refused": 0}
    assert G.fake16("i1") not in json.dumps(inspect(state))
    store.close()
