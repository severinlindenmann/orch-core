"""HUB-AC-03 (#1426): the host tells the hub four references per agent action and nothing else."""

from __future__ import annotations

from types import SimpleNamespace

from orch.host import agentevents

SECRET = "SECRET-PROMPT-do-not-leak"


class FakeStore:
    def __init__(self):
        created = {"seq": 1, "type": "ticket.created", "actor": {"kind": "person", "id": "p"}, "key": "ACME-12"}
        created |= {"title": SECRET, "at": "2026-10-30T10:00:00Z"}
        self.logs = {"workspace": [], "T1": [created]}

    def log_marks(self):
        return {k: (0, 0) for k in self.logs}

    def log_seq(self, log):
        return len(self.logs[log]) if log in self.logs else None

    def events_after(self, log, after, limit=256):
        return [(e, "h") for e in self.logs[log] if e["seq"] > after][:limit]

    def add(self, log, typ, actor, **kw):
        self.logs[log].append(
            {"seq": len(self.logs[log]) + 1, "type": typ, "actor": actor, "at": "2026-10-30T10:01:00Z", **kw}
        )


class Link:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def call(self, op, body):
        if self.fail:
            raise OSError("down")
        self.calls.append((op, body))
        return {}


def test_first_pass_sends_nothing_then_only_agent_lines_with_four_references():
    store, link, state = FakeStore(), Link(), SimpleNamespace(hub_id="h1", mode="full")
    service = SimpleNamespace(store=store)
    agentevents.send(service, state, link)
    assert link.calls == []  # connecting never uploads history
    store.add("T1", "ticket.updated", {"kind": "person", "id": "p"}, note=SECRET)
    store.add("T1", "comment.added", {"kind": "agent", "id": "x y", "session": "sess-1"}, body=SECRET, title=SECRET)
    store.add("workspace", "gate.approved", {"kind": "agent", "id": "ag-2"}, prompt=SECRET)
    agentevents.send(service, state, link)
    [(op, body)] = link.calls
    assert op == "agent_events"
    assert sorted(map(sorted, (e.items() for e in body["events"]))) == sorted(
        map(sorted, (e.items() for e in [
            {"kind": "event", "ts_ms": 1793354460000, "agent": "sess-1", "ticket": "ACME-12"},
            {"kind": "gate.approved", "ts_ms": 1793354460000, "agent": "ag-2", "ticket": ""},
        ]))
    )  # fmt: skip
    assert SECRET not in repr(body)
    assert all(set(e) == {"kind", "ts_ms", "agent", "ticket"} for e in body["events"])
    agentevents.send(service, state, link)
    assert len(link.calls) == 1  # nothing new, nothing sent


def test_a_failed_send_is_retried_and_never_raises():
    store, state = FakeStore(), SimpleNamespace(hub_id="h1", mode="full")
    service = SimpleNamespace(store=store)
    agentevents.send(service, state, Link())
    store.add("T1", "ticket.updated", {"kind": "agent", "id": "a"})
    agentevents.send(service, state, Link(fail=True))
    ok = Link()
    agentevents.send(service, state, ok)
    assert len(ok.calls[0][1]["events"]) == 1


def test_at_most_fifty_per_call_and_the_rest_follow():
    store, state, link = FakeStore(), SimpleNamespace(hub_id="h1", mode="full"), Link()
    service = SimpleNamespace(store=store)
    agentevents.send(service, state, link)
    for _ in range(120):
        store.add("T1", "ticket.updated", {"kind": "agent", "id": "a"})
    for _ in range(3):
        agentevents.send(service, state, link)
    assert [len(b["events"]) for _, b in link.calls] == [50, 50, 20]


def test_a_new_ticket_log_after_the_first_pass_is_not_skipped_and_hubs_have_own_positions():
    store, link = FakeStore(), Link()
    service = SimpleNamespace(store=store)
    agentevents.send(service, SimpleNamespace(hub_id="h1", mode="full"), link)
    store.logs["T2"] = []
    store.add("T2", "ticket.created", {"kind": "agent", "id": "a"}, key="ACME-13")
    agentevents.send(service, SimpleNamespace(hub_id="h1", mode="full"), link)
    assert link.calls[0][1]["events"][0]["ticket"] == "ACME-13"
    agentevents.send(service, SimpleNamespace(hub_id="h2", mode="full"), link)
    assert len(link.calls) == 1


def test_selective_mode_sends_nothing_and_keeps_no_position():
    store, link = FakeStore(), Link()
    service = SimpleNamespace(store=store)
    state = SimpleNamespace(hub_id="h1", mode="selective")
    agentevents.send(service, state, link)
    store.add("T1", "ticket.updated", {"kind": "agent", "id": "a"})
    agentevents.send(service, state, link)
    assert link.calls == [] and not getattr(service, "_agent_events_pos", None)


def test_a_log_unreadable_on_the_first_pass_never_uploads_its_history():
    store, link = FakeStore(), Link()
    service = SimpleNamespace(store=store)
    state = SimpleNamespace(hub_id="h1", mode="full")
    store.add("T1", "ticket.updated", {"kind": "agent", "id": "old"})
    real = store.log_seq
    store.log_seq = lambda log: None if log == "T1" else real(log)
    agentevents.send(service, state, link)
    store.log_seq = real  # now readable, with its old agent line
    agentevents.send(service, state, link)
    assert link.calls == []
    store.add("T1", "ticket.updated", {"kind": "agent", "id": "new"})
    agentevents.send(service, state, link)
    assert [e["agent"] for e in link.calls[0][1]["events"]] == ["new"]


def test_unknown_event_types_are_reported_as_event():
    store, link = FakeStore(), Link()
    service = SimpleNamespace(store=store)
    state = SimpleNamespace(hub_id="h1", mode="full")
    agentevents.send(service, state, link)
    store.add("T1", "something.private_word", {"kind": "agent", "id": "a"})
    agentevents.send(service, state, link)
    assert link.calls[0][1]["events"][0]["kind"] == "event"
