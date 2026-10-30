"""#1285: the workspace side of the hub's webhook inbox: register, pull, ack, the wake limit, the fallback."""

import json

import pytest

from orch.host import hubhooks
from orch.model import followup_events as fe
from tests.host.test_agents import w  # noqa: F401
from tests.host.test_followups_runner import env  # noqa: F401


class FakeLink:
    """A hub link that answers the four hook ops from a list; every call is kept."""

    def __init__(self, rings=(), fail=None):
        self.rings, self.fail, self.calls, self.closed = list(rings), fail, [], 0

    def call(self, op, body):
        self.calls.append((op, body))
        if self.fail:
            raise self.fail
        if op == "hook_put":
            return {"url_base": "https://hub.example/h/"}
        if op == "hook_pull":
            return {"rings": list(self.rings), "head": max((r["seq"] for r in self.rings), default=0)}
        if op == "hook_ack":
            self.rings = [r for r in self.rings if r["seq"] > body["seq"]]  # the hub deletes what is acked
            return {"acked": body["seq"]}
        return {}

    def ops(self):
        return [c[0] for c in self.calls]


def ring(seq, th, source="github"):
    return {"seq": seq, "token_hash": th, "source": source, "kind": "hook.rang", "received_at": 1}


@pytest.fixture
def hub(monkeypatch):
    link = FakeLink()

    def fake(service):
        class T:
            def close(self):
                link.closed += 1

        return link, T()

    monkeypatch.setattr(hubhooks, "_link", fake)
    return link


def register(e, token=None, source="github"):
    token = token or fe.new_hub_token()
    base = hubhooks.register(e.w.service, token=token, secret=fe.new_hub_secret(), source=source, followup="DEMO-0001")
    return token, base


def test_register_sends_hash_and_secret_and_keeps_neither_token_nor_secret(env, hub):  # noqa: F811
    e = env
    token = fe.new_hub_token()
    secret = fe.new_hub_secret()
    base = hubhooks.register(e.w.service, token=token, secret=secret, source="databricks", followup="a/b")
    assert base == "https://hub.example/h/"
    op, body = hub.calls[0]
    assert op == "hook_put" and body["token_hash"] == fe.hub_token_hash(token) and body["secret"] == secret
    assert body["followup"] == "hook" and body["source"] == "databricks"  # an unsafe label is replaced
    assert hub.closed == 1
    disk = hubhooks._path(e.w.service).read_text()
    assert token not in disk and secret not in disk and body["token_hash"] in disk
    assert hubhooks._path(e.w.service).stat().st_mode & 0o777 == 0o600


def test_register_refuses_a_bad_source_or_secret_before_the_hub(env, hub):  # noqa: F811
    with pytest.raises(ValueError):
        hubhooks.register(env.w.service, token=fe.new_hub_token(), secret="x" * 46, source="github", followup="a")
    with pytest.raises(ValueError):
        hubhooks.register(
            env.w.service, token=fe.new_hub_token(), secret=fe.new_hub_secret(), source="slack", followup="a"
        )
    assert hub.calls == []


def test_ring_wakes_the_pending_follow_up_and_is_acked(env, hub):  # noqa: F811
    e = env
    token, _ = register(e)
    on = fe.parse_on("webhook:" + token, e.w.wid)
    uid, _, _, _ = e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()  # learns the pending hook
    hub.calls.clear()
    hub.rings = [ring(7, fe.hub_token_hash(token))]
    e.tick()
    rec = e.view(uid)
    assert rec["state"] == "fired" and rec["trigger"] == "webhook" and not rec.get("fallback")
    assert set(rec["event"]) == {"source", "kind", "ref", "status", "at"} and rec["event"]["status"] == "rang"
    assert "hook_ack" in hub.ops() and [c[1] for c in hub.calls if c[0] == "hook_ack"] == [{"seq": 7}]
    assert hubhooks._load(e.w.service)["wakes"], "an accepted ring counts against the hour"
    assert token not in json.dumps(e.hub.commands)


def test_a_ring_for_a_hash_this_host_never_registered_is_acked_and_ignored(env, hub):  # noqa: F811
    e = env
    token, _ = register(e)
    on = fe.parse_on("webhook:" + token, e.w.wid)
    uid, _, _, _ = e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    hub.rings = [ring(3, fe.hub_token_hash(fe.new_hub_token()))]
    e.tick()
    assert e.view(uid)["state"] == "pending"
    assert [c[1] for c in hub.calls if c[0] == "hook_ack"] == [{"seq": 3}]  # acked, so it does not come back


def test_the_hub_cannot_name_a_followup_or_source(env, hub):  # noqa: F811
    e = env
    token, _ = register(e, source="github")
    th = fe.hub_token_hash(token)
    on = fe.parse_on("webhook:" + token, e.w.wid)
    e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    hub.rings = [{**ring(1, th), "followup": "fu_evil", "source": "databricks"}]
    # the link reply check refuses extra keys; here the stub bypasses it, and the host still reads only token_hash
    hooks, top = hubhooks.pull(e.w.service)
    assert hooks == [fe.hook_hash(token, e.w.wid)] and top == 1


def test_wake_limit_per_hour_then_the_deadline_is_the_fallback(env, hub):  # noqa: F811
    e = env
    token, _ = register(e)
    on = fe.parse_on("webhook:" + token, e.w.wid)
    uid, _, _, deadline = e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    now_ms = int(e.w.service.clock() * 1000)
    doc = hubhooks._load(e.w.service)
    doc["wakes"] = [now_ms - 1000 * i for i in range(hubhooks.WAKES_PER_HOUR)]
    hubhooks._save(e.w.service, doc)
    hub.rings = [ring(5, fe.hub_token_hash(token))]
    e.tick()
    assert e.view(uid)["state"] == "pending"  # the ring was dropped, not woken
    assert [c[1] for c in hub.calls if c[0] == "hook_ack"][-1] == {"seq": 5}
    e.w.lab.ws.clock[0] = deadline
    e.tick()
    rec = e.view(uid)
    assert rec["state"] == "fired" and rec["fallback"] == "no_ring"
    # an hour later the limit is open again for the next ring
    hub.rings = [ring(6, fe.hub_token_hash(token))]
    assert hubhooks.pull(e.w.service) == ([fe.hook_hash(token, e.w.wid)], 6)


def test_a_failing_hub_never_breaks_the_tick_and_the_deadline_still_wakes(env, monkeypatch):  # noqa: F811
    e = env
    link = FakeLink(fail=RuntimeError("hub away"))

    class T:
        def close(self):
            pass

    monkeypatch.setattr(hubhooks, "_link", lambda service: (link, T()))
    token = fe.new_hub_token()
    # registered earlier, hub gone now
    with pytest.raises(RuntimeError):
        hubhooks.register(e.w.service, token=token, secret=fe.new_hub_secret(), source="github", followup="a")
    link.fail = None
    hubhooks.register(e.w.service, token=token, secret=fe.new_hub_secret(), source="github", followup="a")
    link.fail = RuntimeError("hub away")
    on = fe.parse_on("webhook:" + token, e.w.wid)
    uid, _, _, deadline = e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    e.tick()
    assert e.view(uid)["state"] == "pending"
    e.w.lab.ws.clock[0] = deadline
    e.tick()
    assert e.view(uid)["fallback"] == "no_ring"


def test_no_registered_hub_hook_means_no_hub_traffic(env, hub):  # noqa: F811
    e = env
    on = fe.parse_on("webhook:" + fe.new_hook_id(), e.w.wid)
    e.schedule_on(on, "webhook", hook=on["hook"])
    e.tick()
    e.tick()
    assert hub.calls == []


def test_expired_hooks_leave_the_map(env, hub):  # noqa: F811
    e = env
    token, _ = register(e)
    e.w.lab.ws.clock[0] += (hubhooks.HOOK_LIFETIME_DAYS + 1) * 86400
    assert hubhooks.pull(e.w.service) == ([], 0)
    register(e)  # a later register prunes it
    assert len(hubhooks._load(e.w.service)["hooks"]) == 1


def test_revoke_forgets_and_tells_the_hub(env, hub):  # noqa: F811
    e = env
    token, _ = register(e)
    th = fe.hub_token_hash(token)
    hub.calls.clear()
    hubhooks.revoke(e.w.service, th)
    assert hub.calls == [("hook_revoke", {"token_hash": th})] and hubhooks.watching(e.w.service) is False
    hubhooks.revoke(e.w.service, th)  # again: nothing to do, no traffic
    assert len(hub.calls) == 1
