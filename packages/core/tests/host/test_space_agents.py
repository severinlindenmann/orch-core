"""Hub M3 spaces SP-1: ``by`` on every item, the per-space yes with its warning, agents behind the owner's switch,
what an agent may copy out, volume caps, history and restore (Opus conditions 1-8, 10, 12)."""

# ruff: noqa: F401, F811  (the fixtures of test_space_ops are imported, and used by name as arguments)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch import crypto
from orch.hubclient import spaces as SP
from orch.hubclient.errors import HubRefused
from orch.hubclient.spaceclient import _by_kind
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis
from tests.host.test_hub_ops import keystore
from tests.host.test_peer_ops import PEER_WS
from tests.host.test_space_ops import (
    dev_workspace,
    full,
    human,
    lab,
    no_log_sync_sender,
    pin_peer,
    req,
    selective,
    served,
)

PUT = {"name": "notes.md", "data": crypto.b64u(b"hello")}


def service_of(served):
    return next(iter(served.h.host.services.values()))


def agent(served, op, args):
    return served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)


def agent_id(served) -> str:
    return served.grant.split(".")[0]


def owner_id(served) -> str:
    return ok(req(served, "whoami"))["data"]["person"]


def share(served, sh, agents=False):
    ok(req(served, "space.share", {"space_id": sh.space_id, "members": sh.members, "understood": True}))
    if agents:
        ok(req(served, "space.agents", {"space_id": sh.space_id, "on": True}))


def stored(served, sh):
    """Play the hub storing the last put (so history and restore can read it)."""
    sent = sh.puts[-1]
    sh.entries.append(
        {"seq": len(sh.entries) + 1, "item_id": sent["item_id"], "epoch": sent["epoch"], "revoked": False,
         "sealed": sent["sealed"]}
    )  # fmt: skip
    return sent["item_id"]


def opened(served, sh, sent):
    pub = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    return SP.verify_item(
        sent["sealed"], sk=sh.sk, space_id=sh.space_id, epoch=sent["epoch"], item_id=sent["item_id"],
        members=sh.members, wsk_pubs={served.wid: pub}, hub_id=sh.hub.hub_id, dev_mode=True,
    )  # fmt: skip


def security_log(served) -> list[dict]:
    path = Path(service_of(served).state_dir) / "host" / "audit-security.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def put(sid, **kw):
    return {"space_id": sid, **PUT, **kw}


# --- condition 1/7: the contract ------------------------------------------------------------------------------------


def build(body):
    return SP.build_item(
        hub_id="aa" * 16, space_id="bb" * 16, epoch=1, members=["cc" * 16], workspace_id="cc" * 16, body=body,
        created_ms=1, dev=True, sign=lambda m: b"\0" * 64,
    )  # fmt: skip


def test_the_contract_refuses_an_item_without_a_valid_by():
    good = {"name": "n", "size": 1, "data": "AA", "by": "person:p_1"}
    build(good)
    build({**good, "by": "agent:gr_01J9ZP-x_y", "restored_from": "ab" * 16})
    for bad in (
        {k: v for k, v in good.items() if k != "by"},
        {**good, "by": "user:x"},
        {**good, "by": "person:"},
        {**good, "by": "person:a b"},
        {**good, "by": "agent:" + "a" * 65},
        {**good, "by": 3},
        {**good, "restored_from": "nope"},
        {**good, "extra": 1},
    ):
        with pytest.raises(HubRefused) as exc:
            build(bad)
        assert exc.value.code == "malformed"


def test_by_is_shown_as_a_fixed_word():
    assert [_by_kind(x) for x in ("person:p_1", "agent:s", "root", None, 5)] == [
        "person", "agent", "unknown", "unknown", "unknown",
    ]  # fmt: skip


# --- conditions 2 and 6: by comes from the actor only ---------------------------------------------------------------


def test_a_person_put_is_marked_with_the_person_and_args_cannot_set_by(served, full):
    share(served, full)
    for op, args in (
        ("space.put", put(full.space_id, by="person:someone-else")),
        ("space.restore", {"space_id": full.space_id, "item_id": "ab" * 16, "by": "person:x"}),
        ("space.agents", {"space_id": full.space_id, "on": True, "by": "x"}),
    ):
        assert err(req(served, op, args)) == "invalid.input"
    assert full.puts == []
    ok(req(served, "space.put", put(full.space_id)))
    assert opened(served, full, full.puts[0])["body"]["by"] == f"person:{owner_id(served)}"


def test_an_agent_put_is_marked_with_the_verified_grant_whatever_the_session_says(served, full):
    share(served, full, agents=True)
    res = served.client().req("space.put", put(full.space_id), grant=served.grant, session="s_FORGED", idem=True)
    ok(res)
    assert opened(served, full, full.puts[0])["body"]["by"] == f"agent:{agent_id(served)}"
    assert err(agent(served, "space.put", put(full.space_id, by="agent:x"))) == "invalid.input"


# --- condition 1: the share rule, the warning and the hub -----------------------------------------------------------


def test_share_needs_understood_and_stores_hub_warned_and_agents_off(served, full):
    for extra in ({}, {"understood": False}, {"understood": "yes"}):
        args = {"space_id": full.space_id, "members": full.members, **extra}
        assert err(req(served, "space.share", args)) == "invalid.input"
    got = ok(req(served, "space.share", {"space_id": full.space_id, "members": full.members, "understood": True}))
    assert got["data"]["agents"] is False and "hub and its admins can read" in got["data"]["warning"]
    from orch.host import spaceops

    rule = spaceops._load_state(service_of(served)).rules[full.space_id]  # noqa: SLF001
    assert rule == {"members": full.members, "hub_id": full.hub.hub_id, "warned": True, "agents": False}


def test_a_rule_given_for_another_hub_is_not_shared(served, full):
    from orch.host import spaceops

    share(served, full)
    svc = service_of(served)
    st = spaceops._load_state(svc)  # noqa: SLF001
    st.rules[full.space_id]["hub_id"] = "ee" * 16
    spaceops._saver(svc)(st)  # noqa: SLF001
    res = req(served, "space.put", put(full.space_id))
    assert err(res) == "invalid.input" and "not_shared" in res["error"]["message"] and full.puts == []


# --- condition 2/4/5: the agents switch -----------------------------------------------------------------------------


def test_agents_are_off_until_the_owner_turns_them_on(served, full):
    pin_peer(served)
    sid = full.space_id
    assert "not_shared" in agent(served, "space.put", put(sid))["error"]["message"]  # no rule at all
    share(served, full)
    res = agent(served, "space.put", put(sid))
    assert err(res) == "invalid.input" and "agents_off" in res["error"]["message"] and full.puts == []
    on = ok(req(served, "space.agents", {"space_id": sid, "on": True}))["data"]
    assert on["agents"] is True and "Agents can copy workspace content here" in on["warning"]
    ok(agent(served, "space.put", put(sid)))
    assert len(full.puts) == 1
    ok(req(served, "space.agents", {"space_id": sid, "on": False}))
    assert "agents_off" in agent(served, "space.put", put(sid))["error"]["message"] and len(full.puts) == 1


def test_an_agent_can_not_switch_share_or_revoke_and_a_bad_grant_is_no_agent(served, full):
    sid = full.space_id
    for op, args in (
        ("space.agents", {"space_id": sid, "on": True}),
        ("space.share", {"space_id": sid, "members": full.members, "understood": True}),
        ("space.revoke", {"space_id": sid, "item_id": "ab" * 16}),
    ):
        assert err(agent(served, op, args)) == "op.not_allowed"
    share(served, full, agents=True)
    bad = served.client().req("space.put", put(sid), grant=served.grant[:-3] + "AAA", session=SESSION, idem=True)
    assert err(bad) != "" and full.puts == []


def test_a_new_member_stops_agents_until_the_owner_shares_again(served, full):
    """Condition 10: the console adds a member (members_changed); an agent put is refused until a new share, which
    resets agents to Off."""
    share(served, full, agents=True)
    ok(agent(served, "space.put", put(full.space_id)))
    full.members, full.epoch = sorted([*full.members, "cc" * 16]), 2
    res = agent(served, "space.put", put(full.space_id))
    assert err(res) == "invalid.input" and "members_changed" in res["error"]["message"] and len(full.puts) == 1
    ok(req(served, "space.share", {"space_id": full.space_id, "members": full.members, "understood": True}))
    res = agent(served, "space.put", put(full.space_id))
    assert "agents_off" in res["error"]["message"] and len(full.puts) == 1
    ok(req(served, "space.agents", {"space_id": full.space_id, "on": True}))
    ok(agent(served, "space.put", put(full.space_id)))


# --- condition 3: nothing the host holds leaves through an agent ----------------------------------------------------


def test_an_agent_cannot_copy_a_secret_the_host_holds(served, full):
    import base64

    from orch.addons import credential

    svc = service_of(served)
    pat = "tok-NOT-A-SHAPE-9f8e7d6c5b4a39281706"  # held, but with no GitHub token shape: found by value
    credential.person_store(svc.state_dir, svc.workspace_id, svc.host_uid, "github", pat, 1.0)
    keyfile = next((Path(svc.state_dir) / "hosts" / svc.workspace_id / "keys").glob("*.filekey.json"))
    scalar = json.loads(keyfile.read_text())["d"]
    share(served, full, agents=True)
    leaks = {
        "raw": pat.encode(),
        "inside text": b"export TOKEN=" + pat.encode() + b"\n",
        "base64": base64.b64encode(pat.encode()),
        "urlsafe base64": base64.urlsafe_b64encode(pat.encode()).rstrip(b"="),
        "key file value": scalar.encode(),
        "key bytes": crypto.unb64u(scalar),
        "its own grant": served.grant.encode(),
        "a github token shape": b"ghp_" + b"a" * 36,
        "a private key block": b"-----BEGIN EC PRIVATE KEY-----\nAAAA",
    }
    for n, (why, data) in enumerate(leaks.items(), 1):
        res = agent(served, "space.put", put(full.space_id, data=crypto.b64u(data)))
        assert "secret_in_content" in res["error"]["message"], why
        assert full.puts == [], why
        assert refused_count(served) == n, why  # exactly one audit line per refusal
    res = agent(served, "space.put", put(full.space_id, name=pat, data=crypto.b64u(b"x")))
    assert full.puts == []
    refused = [e for e in security_log(served) if e["event"] == "space.secret_refused"]
    assert refused and all("sha256" not in e and pat not in json.dumps(e) for e in refused)
    # the same text from the owner at the terminal is the owner's business: not checked
    ok(req(served, "space.put", put(full.space_id, data=crypto.b64u(b"x" + pat.encode()))))
    assert len(full.puts) == 1


# --- condition 4: volume and audit ----------------------------------------------------------------------------------


@pytest.mark.slow
def test_an_agent_session_is_capped_at_20_items_and_1_mb_an_hour_and_every_put_is_audited(served, full, monkeypatch):
    from orch.host import spaceops

    monkeypatch.setattr(spaceops, "AGENT_PUTS_PER_MINUTE", 1000)
    share(served, full, agents=True)
    for i in range(20):
        ok(agent(served, "space.put", put(full.space_id, name=f"f{i}")))
    res = agent(served, "space.put", put(full.space_id, name="f20"))
    assert err(res) == "invalid.input" and "rate_limit" in res["error"]["message"] and len(full.puts) == 20
    log = [e for e in security_log(served) if e["event"] == "space.agent_put"]
    assert len(log) == 20 and log[0]["name"] == "f0" and log[0]["size"] == 5 and log[0]["session"] == SESSION
    import hashlib

    assert log[0]["sha256"] == hashlib.sha256(b"hello").hexdigest() and log[0]["space_id"] == full.space_id
    assert log[0]["agent"] == agent_id(served)


def test_the_hour_has_a_byte_cap_and_a_minute_cap(served, full, monkeypatch):
    from orch.host import spaceops

    monkeypatch.setattr(spaceops, "AGENT_BYTES_PER_HOUR", 100)
    share(served, full, agents=True)
    ok(agent(served, "space.put", put(full.space_id, name="a", data=crypto.b64u(b"x" * 60))))
    res = agent(served, "space.put", put(full.space_id, name="b", data=crypto.b64u(b"x" * 60)))
    assert "rate_limit" in res["error"]["message"] and len(full.puts) == 1
    monkeypatch.setattr(spaceops, "AGENT_BYTES_PER_HOUR", 1 << 20)
    monkeypatch.setattr(spaceops, "AGENT_PUTS_PER_MINUTE", 2)
    ok(agent(served, "space.put", put(full.space_id, name="c")))
    assert "rate_limit" in agent(served, "space.put", put(full.space_id, name="d"))["error"]["message"]


def test_a_refused_put_does_not_use_up_the_hour(served, full):
    from orch.host import spaceops

    share(served, full, agents=True)
    full.members, full.epoch = sorted([*full.members, "cc" * 16]), 2
    for _ in range(spaceops.AGENT_ITEMS_PER_HOUR + 2):
        assert "members_changed" in agent(served, "space.put", put(full.space_id))["error"]["message"]


# --- conditions 8 and 12: history and restore -----------------------------------------------------------------------


def test_history_lists_versions_newest_first_without_content(served, full, monkeypatch):
    pin_peer(served)
    share(served, full, agents=True)
    svc = service_of(served)
    ticks = iter(range(1_790_000_100, 1_790_000_200))
    monkeypatch.setattr(svc, "clock", lambda: float(next(ticks)))
    ok(req(served, "space.put", put(full.space_id, name="plan.md")))
    first = stored(served, full)
    ok(agent(served, "space.put", put(full.space_id, name="plan.md", data=crypto.b64u(b"v2!"))))
    second = stored(served, full)
    full.peer_item("plan.md", b"theirs", created_ms=1, by="agent:s_x")
    third = full.entries[-1]["item_id"]
    full.peer_item("other.md", b"x")
    for who in (req, agent):
        rows = ok(who(served, "space.history", {"space_id": full.space_id, "name": "plan.md"}))["data"]
        v = rows["versions"]
        assert [r["item_id"] for r in v] == [second, first, third]
        assert [(r["by"], r["size"]) for r in v] == [("agent", 3), ("person", 5), ("agent", 6)]
        assert [r["author"] for r in v] == [served.wid, served.wid, PEER_WS]
        assert all(set(r) == {"item_id", "created_ms", "author", "by", "revoked", "size"} for r in v)
        assert rows["untrusted"] is True and rows["revoked_unnamed"] == 0


def test_restore_puts_the_old_bytes_again_as_a_new_item(served, full):
    pin_peer(served)
    share(served, full)
    ok(req(served, "space.put", put(full.space_id, name="plan.md")))
    old = stored(served, full)
    got = ok(req(served, "space.restore", {"space_id": full.space_id, "item_id": old}))["data"]
    assert got["restored_from"] == old and got["name"] == "plan.md" and got["item_id"] != old
    body = opened(served, full, full.puts[-1])["body"]
    assert crypto.unb64u(body["data"]) == b"hello" and body["name"] == "plan.md"
    assert body["restored_from"] == old and body["by"] == f"person:{owner_id(served)}"


def test_only_our_own_live_verified_items_are_restorable(served, full):
    pin_peer(served)
    share(served, full)
    ok(req(served, "space.put", put(full.space_id)))
    mine = stored(served, full)
    full.peer_item("theirs.md", b"x")
    theirs = full.entries[-1]["item_id"]
    full.entries.append({"seq": 50, "item_id": mine, "epoch": 1, "revoked": True})  # revoked later by us
    for item in (mine, theirs, "ab" * 16):
        res = req(served, "space.restore", {"space_id": full.space_id, "item_id": item})
        assert err(res) == "invalid.input" and "not_restorable" in res["error"]["message"], item
    assert len(full.puts) == 1


def test_an_unverifiable_item_is_not_restorable(served, full):
    share(served, full)  # no pin of the peer: its item cannot be verified
    full.peer_item("x.md", b"x")
    res = req(served, "space.restore", {"space_id": full.space_id, "item_id": full.entries[-1]["item_id"]})
    assert "not_restorable" in res["error"]["message"] and full.puts == []


def test_an_agent_restore_is_behind_the_switch_and_the_content_check(served, full):
    from orch.addons import credential

    share(served, full)
    ok(req(served, "space.put", put(full.space_id)))
    old = stored(served, full)
    args = {"space_id": full.space_id, "item_id": old}
    assert "agents_off" in agent(served, "space.restore", args)["error"]["message"]
    ok(req(served, "space.agents", {"space_id": full.space_id, "on": True}))
    ok(agent(served, "space.restore", args))
    body = opened(served, full, full.puts[-1])["body"]
    assert body["by"] == f"agent:{agent_id(served)}" and body["restored_from"] == old
    log = [e for e in security_log(served) if e["event"] == "space.restore"]
    assert log and log[-1]["agent"] == agent_id(served)
    # a held secret in the old content is not copied again by an agent
    svc = service_of(served)
    credential.person_store(svc.state_dir, svc.workspace_id, svc.host_uid, "github", "hello", 1.0)  # too short: ignored
    ok(agent(served, "space.restore", args))


# --- review fixes ----------------------------------------------------------------------------------------------------


def hold(served, name, doc):
    svc = service_of(served)
    d = Path(svc.state_dir) / "hosts" / svc.workspace_id / "membership" / "keys"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(doc))
    path.chmod(0o600)


def refused_count(served):
    return sum(1 for e in security_log(served) if e["event"] == "space.secret_refused")


def test_the_relay_epoch_keys_of_the_membership_store_are_held_secrets(served, full):
    import base64

    wk = crypto.b64u(bytes(range(40, 72)))
    hold(served, "wk-1.json", {"wk": wk})
    share(served, full, agents=True)
    for data in (wk.encode(), crypto.unb64u(wk), base64.b64encode(wk.encode())):
        res = agent(served, "space.put", put(full.space_id, data=crypto.b64u(data)))
        assert "secret_in_content" in res["error"]["message"]
    assert full.puts == [] and refused_count(served) == 3


def test_a_secret_is_found_at_every_base64_alignment_in_hex_and_inside_an_encoded_run(served, full):
    import base64

    from orch.host import heldsecrets

    secret = b"s3cr3t-value-NOT-shaped-0123456789"
    hold(served, "ck-1.json", {"ck": secret.decode()})
    share(served, full, agents=True)
    seen = []
    for k in (0, 1, 2):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            seen.append(b"x" + enc(b"Q" * k + secret + b"zz") + b" tail")
    seen += [b"key=" + secret.hex().encode(), secret.hex().upper().encode()]
    seen.append(base64.b64encode(b"export A=" + secret + b"\n"))  # an encoded run that CONTAINS it
    seen.append(secret.hex().encode().join((b"abc ", b" def")))
    for n, data in enumerate(seen, 1):
        res = agent(served, "space.put", put(full.space_id, data=crypto.b64u(data)))
        assert "secret_in_content" in res["error"]["message"], data
        assert refused_count(served) == n
    assert full.puts == []
    # forms under 8 characters are never derived: a short secret matches nothing it should not
    assert heldsecrets.find(b"abc", [b"abcdefgh"]) is False
    assert heldsecrets.find(b"hello world, nothing here", [secret]) is False
    # a proxy capability is a secret whatever its value
    assert heldsecrets.find(b"x ocap_" + b"A" * 43, [])


def test_turning_agents_off_needs_no_hub_and_no_member_check(served, full):
    share(served, full, agents=True)
    full.members, full.epoch = sorted([*full.members, "cc" * 16]), 2  # a member was added behind the owner's back
    ok(req(served, "space.agents", {"space_id": full.space_id, "on": False}))
    assert "members_changed" in req(served, "space.agents", {"space_id": full.space_id, "on": True})["error"]["message"]
    from orch.host import spaceops

    assert spaceops._load_state(service_of(served)).rules[full.space_id]["agents"] is False  # noqa: SLF001
    share2 = {"space_id": full.space_id, "members": full.members, "understood": True}
    ok(req(served, "space.share", share2))
    ok(req(served, "space.agents", {"space_id": full.space_id, "on": True}))
    full.hub.close()  # the hub is gone
    assert ok(req(served, "space.agents", {"space_id": full.space_id, "on": False}))["data"]["agents"] is False
    assert req(served, "space.agents", {"space_id": full.space_id, "on": True}).get("error")


def test_an_agent_restore_is_audited_like_a_put(served, full):
    import hashlib

    share(served, full, agents=True)
    ok(req(served, "space.put", put(full.space_id, name="plan.md")))
    old = stored(served, full)
    ok(agent(served, "space.restore", {"space_id": full.space_id, "item_id": old}))
    (rec,) = [e for e in security_log(served) if e["event"] == "space.agent_restore"]
    assert (rec["name"], rec["size"], rec["session"], rec["agent"]) == ("plan.md", 5, SESSION, agent_id(served))
    assert rec["sha256"] == hashlib.sha256(b"hello").hexdigest() and rec["restored_from"] == old
