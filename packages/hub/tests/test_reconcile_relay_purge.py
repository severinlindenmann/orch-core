"""#52: a revoke made by another process (the CLI) is purged at the relay by the running hub's reconcile tick."""

from __future__ import annotations

from test_relay_client import PUSH_PUB, TOKEN, w  # noqa: F401  (the fixture)

from orch_hub.reconcile import reconcile_relay_purge
from orch_hub.relay.notify import PushNotifier


def _cli_revoked(w):  # noqa: F811
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)  # the serving hub's notifier; its on_revoke hook is NOT wired to the CLI
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.client.mailbox_put(cid, "ee" * 16, "AAAA")
    w.env.svc.revoke(cid)  # "the CLI process": no hook runs here
    assert cid in w.relay.targets and w.relay.mailbox
    return n, cid


def test_a_cli_revoke_is_purged_at_the_relay_within_one_tick(w):  # noqa: F811
    n, cid = _cli_revoked(w)
    n.purge_pending()  # what reconcile_relay_purge starts
    assert w.relay.targets == {} and w.relay.mailbox == {} and cid in w.relay.revoked
    assert w.env.svc.unpurged_revoked() == []


def test_the_tick_runs_through_reconcile_and_a_none_notifier_is_a_noop(w):  # noqa: F811
    reconcile_relay_purge(None)
    n, cid = _cli_revoked(w)
    reconcile_relay_purge(n)
    n._purging.acquire()  # waits for the background pass to end
    n._purging.release()
    assert w.relay.mailbox == {} and cid in w.relay.revoked


def test_a_failed_purge_is_retried_each_tick_then_done_exactly_once(w):  # noqa: F811
    n, cid = _cli_revoked(w)
    w.relay.faults = [OSError("down")] * 8
    n.purge_pending()
    assert w.env.svc.unpurged_revoked() == [cid] and cid in w.relay.targets  # not marked: retried
    w.relay.faults.clear()
    n.purge_pending()
    assert w.env.svc.unpurged_revoked() == [] and w.relay.targets == {}
    before = len(w.relay.log)
    n.purge_pending()
    n.purge_pending()
    assert len(w.relay.log) == before  # marked: no further relay call


def test_the_in_process_hook_marks_it_so_the_tick_does_not_repeat_it(w):  # noqa: F811
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    w.env.svc.on_revoke = n.channel_revoked
    w.env.svc.revoke(cid)
    before = len(w.relay.log)
    n.purge_pending()
    assert len(w.relay.log) == before and w.env.svc.unpurged_revoked() == []
