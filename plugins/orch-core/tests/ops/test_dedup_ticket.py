"""Retry dedup and the stop rule are per resolved ticket, with or without a REF (F1 10.4 item 9)."""

from __future__ import annotations


def _two_tickets(cli):
    cli("new", "A")
    cli("new", "B")


def _types(ws, ref):
    return [e["type"] for e in ws.events(ref)]


def test_a_ref_less_write_on_another_claimed_ticket_is_not_a_duplicate(ws, cli):
    _two_tickets(cli)
    assert cli("claim", "1").code == 0
    a = cli("section", "set", "context", "-m", "X")
    assert a.code == 0 and "duplicate" not in a.out
    assert cli("release").code == 0
    assert cli("claim", "2").code == 0
    n_b = len(ws.events("2"))
    b = cli("section", "set", "context", "-m", "X")  # same words, now resolving to ticket 2
    assert b.code == 0 and "duplicate" not in b.out, b.out + b.err
    assert len(ws.events("2")) == n_b + 1


def test_a_ref_less_retry_on_the_same_ticket_is_still_a_duplicate(ws, cli):
    _two_tickets(cli)
    cli("claim", "1")
    n = len(ws.events("1"))
    first = cli("log", "same note")
    again = cli("log", "same note")
    assert "duplicate" not in first.out and again.out.splitlines()[0].endswith(" duplicate")
    assert len(ws.events("1")) == n + 1


def test_the_stop_rule_counts_per_resolved_ticket(ws, cli):
    _two_tickets(cli)
    cli("claim", "1")
    for _ in range(2):
        r = cli("submit")  # refused the same way, on ticket 1
        assert r.code != 0 and "err stop" not in r.err, r.err
    cli("release")
    cli("claim", "2")
    r = cli("submit")  # ticket 2: its own count, not the third refusal
    assert r.code != 0 and "err stop" not in r.err, r.err
    for _ in range(2):  # and ticket 2 reaches the stop rule on its own
        r = cli("submit")
    assert r.err.startswith("err stop"), r.err
