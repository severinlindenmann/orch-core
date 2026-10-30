"""#1285: `orch webhook new` with a bound hub that has a public inbox: the address lives at the hub."""

import dataclasses
import json

import pytest

from orch.model import followup_events as fe
from orch.ops.caps import Capabilities
from tests.ops.test_follow_up import schedules  # noqa: F401
from tests.ops.test_task_ac import claimed


@pytest.fixture
def hub(monkeypatch):
    """Every CLI call gets a host-style ``hub_hooks`` capability backed by a recorder."""
    calls, state = [], {"fail": False}

    def register(**kw):
        calls.append(kw)
        if state["fail"]:
            raise RuntimeError("no hub")
        return "https://hub.example/h/"

    orig = Capabilities.direct.__func__

    def direct(cls, env=None, clock=None):
        caps = orig(cls, env) if clock is None else orig(cls, env, clock)
        return dataclasses.replace(caps, hub_hooks=register)

    monkeypatch.setattr(Capabilities, "direct", classmethod(direct))
    return NS(calls=calls, state=state)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_new_registers_at_the_hub_and_the_follow_up_binds_to_the_token(ws, cli, schedules, hub):  # noqa: F811
    claimed(cli)
    r = cli.j("webhook", "new", "1", "--label", "backfill done", "--source", "github")
    assert r.code == 0, r.out
    token, secret = r.data["id"], r.data["secret"]
    assert fe.HUB_TOKEN_RE.fullmatch(token) and fe.HUB_SECRET_RE.fullmatch(secret)
    assert r.data["url"] == f"https://hub.example/h/{token}"
    (call,) = hub.calls
    assert call["token"] == token and call["secret"] == secret and call["source"] == "github"
    text = cli("webhook", "new", "1", "--label", "again", "--source", "github")
    assert "Content type = application/json" in text.out and "secret: hs_" in text.out
    bound = cli.j("follow-up", "--in", "2d", "--on", f"webhook:{token}", "--brief", "after the ring")
    assert bound.code == 0 and bound.data["trigger"] == "webhook", bound.out
    rec = ws.view("1").followups[0]
    assert rec["hook_prefix"] == fe.hook_hash(token, ws.store.workspace_id)[:8]
    ledger = json.dumps(ws.events("1"))
    assert token not in ledger and secret not in ledger  # only the hash is in the ledger


def test_default_source_is_generic_and_bad_source_is_refused(ws, cli, schedules, hub):  # noqa: F811
    claimed(cli)
    assert cli.j("webhook", "new", "1", "--label", "x").code == 0
    assert hub.calls[-1]["source"] == "generic"
    assert cli.j("webhook", "new", "1", "--label", "x", "--source", "slack").code != 0


def test_a_hub_that_refuses_falls_back_to_the_direct_receiver(ws, cli, schedules, hub):  # noqa: F811
    claimed(cli)
    hub.state["fail"] = True
    r = cli.j("webhook", "new", "1", "--label", "x")
    assert r.err_code == "followup.no_receiver", r.out  # no direct receiver either
    cli.env_kw["ORCH_HOOK_BASE"] = "https://hooks.example:9000/"
    r = cli.j("webhook", "new", "1", "--label", "x")
    assert r.code == 0 and r.data["url"].startswith("https://hooks.example:9000/h/hk_") and "secret" not in r.data


def test_the_secret_is_not_part_of_an_idempotent_replay_record(ws, cli, schedules, hub):  # noqa: F811
    from orch.host.outcomes import _mask

    assert "hs_" + "A" * 43 not in json.dumps(_mask({"secret": "hs_" + "A" * 43, "id": "hh_" + "B" * 43}))
