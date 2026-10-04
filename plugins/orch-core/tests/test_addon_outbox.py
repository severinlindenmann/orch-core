import pytest

from addon_fixtures import loaded
from orch.addons import outbox
from orch.addons.runner import AddonRunError
from orch.core.events import append_event


class Mirror:
    def __init__(self, ack=None, fail_event=False, fail_drain=False):
        self.seen, self.drained = [], []
        self._ack, self.fail_event, self.fail_drain = ack, fail_event, fail_drain

    def on_event(self, event, box):
        if self.fail_event and event.kind == "log.added":
            raise RuntimeError("bad event")
        self.seen.append(event.kind)
        box.put({"kind": event.kind, "ticket": event.ticket})

    def drain(self, ctx, items):
        if self.fail_drain:
            raise RuntimeError("offline")
        self.drained.append([i["data"]["kind"] for i in items])
        return [i["id"] for i in items][: self._ack] if self._ack is not None else [i["id"] for i in items]


def _addon(ws, obj):
    return loaded(ws, obj, capabilities=["provider", "page", "settings", "events"])


def test_first_pump_starts_at_now(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    m = Mirror()
    a = _addon(ws, m)
    assert outbox.pump(ws, a) == 0 and m.seen == []
    append_event(ws, "L-1", "claim.taken", agent)
    assert outbox.pump(ws, a) == 1 and m.seen == ["claim.taken"]
    assert outbox.pump(ws, a) == 0


def test_drain_acks_and_keeps_the_rest(ws, agent):
    m = Mirror(ack=1)
    a = _addon(ws, m)
    outbox.pump(ws, a)
    append_event(ws, "L-1", "claim.taken", agent)
    append_event(ws, "L-1", "state.updated", agent)
    outbox.pump(ws, a)
    assert outbox.drain(ws, a) == 1
    assert [i["data"]["kind"] for i in outbox.Outbox(outbox.outbox_path(ws, "demo")).pending()] == ["state.updated"]


def test_failing_on_event_is_logged_and_skipped(ws, agent):
    m = Mirror(fail_event=True)
    a = _addon(ws, m)
    outbox.pump(ws, a)
    append_event(ws, "L-1", "log.added", agent)
    append_event(ws, "L-1", "claim.taken", agent)
    assert outbox.pump(ws, a) == 2 and m.seen == ["claim.taken"]
    assert "bad event" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_failing_drain_keeps_items(ws, agent):
    a = _addon(ws, Mirror(fail_drain=True))
    outbox.pump(ws, a)
    append_event(ws, "L-1", "claim.taken", agent)
    outbox.pump(ws, a)
    assert outbox.drain(ws, a) == 0
    assert len(outbox.Outbox(outbox.outbox_path(ws, "demo")).pending()) == 1


def test_on_event_cannot_run_commands(ws, agent):
    class Sneaky:
        def __init__(self, ctx):
            self.ctx = ctx

        def on_event(self, event, box):
            self.ctx.run(["git", "status"])

        def drain(self, ctx, items):
            return []
    a = _addon(ws, None)
    a.obj = Sneaky(a.ctx.provider_context())
    outbox.pump(ws, a)
    append_event(ws, "L-1", "claim.taken", agent)
    outbox.pump(ws, a)
    assert "while a page renders" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_addons_without_events_are_skipped(ws, agent):
    m = Mirror()
    a = loaded(ws, m)
    assert outbox.pump(ws, a) == 0 and outbox.drain(ws, a) == 0


def test_put_needs_json(tmp_path):
    with pytest.raises(ValueError):
        outbox.Outbox(tmp_path / "o.jsonl").put({"x": object()})


def test_drain_returning_garbage_keeps_items(ws, agent):
    class Odd(Mirror):
        def drain(self, ctx, items):
            return 3
    a = _addon(ws, Odd())
    outbox.pump(ws, a)
    append_event(ws, "L-1", "claim.taken", agent)
    outbox.pump(ws, a)
    assert outbox.drain(ws, a) == 0
    assert len(outbox.Outbox(outbox.outbox_path(ws, "demo")).pending()) == 1
    assert "drain" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_pump_all_keeps_going_after_one_addon_fails(ws, agent, monkeypatch):
    from orch.addons.loader import AddonRegistry
    good = Mirror()
    reg = AddonRegistry(ws, {"bad": _addon(ws, Mirror()), "demo": _addon(ws, good)})
    reg.addons["bad"].name = "bad"
    outbox.pump_all(ws, reg)
    append_event(ws, "L-1", "claim.taken", agent)
    real_pump = outbox.pump

    def pump(ws_, loaded):
        if loaded.name == "bad":
            raise RuntimeError("broken cursor")
        return real_pump(ws_, loaded)
    monkeypatch.setattr(outbox, "pump", pump)
    outbox.pump_all(ws, reg)
    assert good.drained == [["claim.taken"]]
    assert "broken cursor" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_cursor_past_the_log_restarts_at_now(ws, agent):
    m = Mirror()
    a = _addon(ws, m)
    outbox.pump(ws, a)
    outbox._cursor_path(ws, "demo").write_text("999\n", encoding="utf-8")
    append_event(ws, "L-1", "claim.taken", agent)
    outbox.pump(ws, a)
    append_event(ws, "L-1", "state.updated", agent)
    assert outbox.pump(ws, a) == 1 and m.seen == ["state.updated"]


def test_a_hanging_addon_does_not_stall_the_others(ws, agent):
    import asyncio
    import threading
    from orch.addons.loader import AddonRegistry
    release, calls = threading.Event(), []

    class Hanging(Mirror):
        def on_event(self, event, box):
            calls.append(event.kind)
            release.wait(5)
    good = Mirror()
    hanging = loaded(ws, Hanging(), name="slow", capabilities=["provider", "page", "settings", "events"])
    reg = AddonRegistry(ws, {"demo": _addon(ws, good), "slow": hanging})
    outbox.pump_all(ws, reg)  # first pump: both cursors start at now
    pumper = outbox.OutboxPump(ws, event_timeout=0.1, drain_timeout=0.1)

    async def main():
        append_event(ws, "L-1", "claim.taken", agent)
        await pumper.run_round(reg)
        await asyncio.sleep(0.4)
        first = (list(good.seen), "slow" in pumper.busy)
        append_event(ws, "L-1", "state.updated", agent)
        started = pumper.start_round(reg)  # the hanging addon is skipped (single-flight)
        await asyncio.sleep(0.3)
        release.set()
        await asyncio.sleep(0.2)
        return first, started
    (seen, slow_busy), started = asyncio.run(main())
    assert seen == ["claim.taken"] and slow_busy and started == ["demo"]
    assert good.seen == ["claim.taken", "state.updated"] and calls == ["claim.taken"]
    assert "slow" not in pumper.busy
    assert "timed out" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_repumping_an_event_does_not_add_it_twice(ws, agent):
    m = Mirror()
    a = _addon(ws, m)
    outbox.pump(ws, a)
    cursor = outbox._cursor_path(ws, "demo").read_text(encoding="utf-8")
    append_event(ws, "L-1", "claim.taken", agent)
    outbox.pump(ws, a)
    outbox._cursor_path(ws, "demo").write_text(cursor, encoding="utf-8")  # as if the cursor was never saved
    outbox.pump(ws, a)
    items = outbox.Outbox(outbox.outbox_path(ws, "demo")).pending()
    assert m.seen == ["claim.taken", "claim.taken"] and len(items) == 1 and items[0]["id"].startswith("demo-")


def test_disabled_addon_starts_at_now_when_enabled_again(ws, agent):
    from orch.addons.loader import AddonRegistry
    m = Mirror()
    a = _addon(ws, m)
    outbox.pump_all(ws, AddonRegistry(ws, {"demo": a}))
    outbox.pump_all(ws, AddonRegistry(ws, {}))  # disabled: its cursor is dropped
    append_event(ws, "L-1", "claim.taken", agent)  # happens while disabled
    outbox.pump_all(ws, AddonRegistry(ws, {"demo": a}))  # enabled again: starts at now
    append_event(ws, "L-1", "state.updated", agent)
    outbox.pump_all(ws, AddonRegistry(ws, {"demo": a}))
    assert m.seen == ["state.updated"]


def test_cursors_are_kept_when_imports_are_off(ws, agent):
    from orch.addons.loader import AddonRegistry
    a = _addon(ws, Mirror())
    outbox.pump(ws, a)
    outbox.forget_cursors(ws, AddonRegistry(ws, {}, imported=False))
    assert outbox._cursor_path(ws, "demo").exists()
