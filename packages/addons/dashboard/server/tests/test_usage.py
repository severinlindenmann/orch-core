"""Usage (add-on): session time from the host's own run records, a ledger for the month, tokens and cost never invented,
the route answers only while the add-on is on."""

import json

import pytest
from conftest import ORIGIN

from orch_dashboard import usage
from orch_dashboard.gateway import Gateway

NOW = usage.parse("2026-10-18T12:00:00Z")
R1, R2, R3, R4 = (f"r_{c * 26}" for c in "0123")


def run(rid, started, ended=None, ticket="DEMO-1", state=None, **extra):
    row = {
        "run": rid, "kind": "agent", "ticket": ticket, "harness": "claude", "started_at": started,
        "state": state or ("exited" if ended else "running"),
        "exit": {"code": 0, "signal": None, "at": ended, "reason": "exited", "cleanup": "complete"} if ended else None,
    }  # fmt: skip
    return {**row, **extra}


def test_parse_and_money_accept_only_what_is_valid():
    assert usage.parse("2026-10-18T12:00:00Z") == usage.parse("2026-10-18T12:00:00.000Z")
    for bad in (None, 5, "2026-10-18 12:00:00", "2026-13-18T12:00:00Z", "2026-10-18T12:00:00+02:00"):
        assert usage.parse(bad) is None
    assert usage.money(1250, "CHF") == {"cents": 1250, "currency": "CHF"}
    for cents, cur in ((12.5, "CHF"), (True, "CHF"), (-1, "CHF"), (5, "chf"), (5, None), ("5", "EUR")):
        assert usage.money(cents, cur) is None


def test_fact_takes_host_times_and_drops_what_cannot_be_right():
    ok = usage.fact(run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"))
    assert ok["end"] - ok["start"] == 1800 and not ok["live"]
    assert usage.fact(run(R1, "2026-10-18T10:00:00Z", "2026-10-18T09:00:00Z"))["end"] is None  # ends before it starts
    assert usage.fact(run(R1, "2026-09-01T10:00:00Z", "2026-10-18T09:00:00Z"))["end"] is None  # absurdly long
    assert usage.fact({**run(R1, "2026-10-18T10:00:00Z"), "kind": "shell"}) is None
    assert usage.fact(run("bad", "2026-10-18T10:00:00Z")) is None
    assert usage.fact(run(R1, "not a time")) is None
    assert usage.fact(run(R1, "2026-10-18T10:00:00Z", ticket="../x"))["ticket"] is None
    assert usage.fact(run(R1, "2026-10-18T10:00:00Z", harness="Bad Name"))["harness"] is None


def facts(*rows):
    return [usage.fact(r) for r in rows]


def test_windows_count_runs_and_time_per_ticket_and_total():
    fs = facts(
        run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"),
        run(R2, "2026-10-12T10:00:00Z", "2026-10-12T11:00:00Z", ticket="DEMO-2"),
        run(R3, "2026-10-01T10:00:00Z", "2026-10-01T12:00:00Z"),
        run(R4, "2026-10-18T11:00:00Z"),  # still running
    )
    shown = {"DEMO-1", "DEMO-2"}
    out = usage.summarize(fs, NOW, NOW - 40 * usage.DAY, shown, {"DEMO-1": "Fix billing"})
    week, month = out["week"], out["month"]
    # R3 ended 17 days ago: not in the week. R4 runs now: its hour so far (11:00 to 12:00) counts.
    assert (week["runs"], week["ms"], week["running"]) == (3, (1800 + 3600 + 3600) * 1000, 1)
    assert (month["runs"], month["running"]) == (4, 1)
    row = next(r for r in month["tickets"] if r["ticket"] == "DEMO-1")
    assert row["runs"] == 3 and row["title"] == "Fix billing"
    assert month["counted_since"] is None


def test_running_time_so_far_is_added_and_marked():
    out = usage.summarize(facts(run(R4, "2026-10-18T11:00:00Z")), NOW, 0, {"DEMO-1"}, {})
    assert out["week"]["ms"] == 3600 * 1000 and out["week"]["running"] == 1


def test_unknown_time_is_counted_as_a_run_but_never_as_time():
    lost = run(R1, "2026-10-18T10:00:00Z", state="lost")
    out = usage.summarize(facts(lost), NOW, 0, {"DEMO-1"}, {})
    assert (out["week"]["runs"], out["week"]["ms"], out["week"]["unknown"]) == (1, 0, 1)


def test_tickets_not_shown_fold_into_other_and_no_ticket_is_its_own_row():
    fs = facts(
        run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z", ticket="SECRET-9"),
        run(R2, "2026-10-18T10:00:00Z", "2026-10-18T10:10:00Z", ticket=None),
    )
    rows = usage.summarize(fs, NOW, 0, {"DEMO-1"}, {})["week"]["tickets"]
    assert [(r["ticket"], r["other"], r["none"]) for r in rows] == [(None, True, False), (None, False, True)]
    assert "SECRET-9" not in json.dumps(rows)


def test_a_window_before_the_ledger_began_says_since_when():
    out = usage.summarize([], NOW, NOW - 10 * usage.DAY, set(), {})
    assert out["week"]["counted_since"] is None and out["month"]["counted_since"] == usage.iso(NOW - 10 * usage.DAY)


def test_ledger_is_idempotent_private_and_keeps_only_ended_runs(tmp_path):
    env = {"ORCH_STATE_DIR": str(tmp_path)}
    fs = facts(run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"), run(R4, "2026-10-18T11:00:00Z"))
    first = usage.remember("p_me", "w1", fs, NOW, env)
    again = usage.remember("p_me", "w1", fs, NOW + 99, env)
    assert list(first["runs"]) == [R1] and again["first_seen"] == NOW and list(again["runs"]) == [R1]
    assert (tmp_path / "dashboard" / usage.FILE).stat().st_mode & 0o777 == 0o600
    assert usage.remember("p_other", "w1", [], NOW, env)["runs"] == {}  # per person
    (tmp_path / "dashboard" / usage.FILE).write_text("{ not json")
    assert usage.remember("p_me", "w1", [], NOW, env)["runs"] == {}  # unreadable: a fresh ledger, never an error


def test_ledger_drops_old_and_excess_rows(tmp_path, monkeypatch):
    env = {"ORCH_STATE_DIR": str(tmp_path)}
    monkeypatch.setattr(usage, "MAX_LEDGER", 2)
    fs = facts(
        run("r_" + "1" * 26, "2026-10-18T09:00:00Z", "2026-10-18T09:10:00Z"),
        run("r_" + "2" * 26, "2026-10-18T10:00:00Z", "2026-10-18T10:10:00Z"),
        run("r_" + "3" * 26, "2026-10-18T11:00:00Z", "2026-10-18T11:10:00Z"),
        run("r_" + "4" * 26, "2024-01-01T09:00:00Z", "2024-01-01T09:10:00Z"),
    )
    assert sorted(usage.remember("p_me", "w1", fs, NOW, env)["runs"]) == ["r_" + "2" * 26, "r_" + "3" * 26]


# -- the route --


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


class Host:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.calls, self.runs, self.on = [], [], True

    def supports(self, op, cap):
        return op == "run.list" and cap == "run/1"

    def call(self, op, args=None, **kw):
        self.calls.append((op, args))
        if op == "addon.list":
            return {"addons": [{"name": "usage", "state": "active" if self.on else "not_granted", "enabled": self.on}]}
        if op == "run.list":
            return {"runs": self.runs, "next_page": None}
        if op == "ticket.query":
            return {"rows": [{"key": "DEMO-1", "title": "Fix billing"}], "next_page": None}
        raise AssertionError(op)

    addon_on = Gateway.addon_on


@pytest.fixture
def host(gateway, monkeypatch, tmp_path):
    fake = Host()
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: fake.call(op, args))
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    monkeypatch.setattr(usage, "now", lambda: NOW)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Gateway, "addon_on", classmethod(lambda cls, link, name: link.on))
    return fake


def test_route_gives_time_per_ticket_and_never_tokens_or_cost(client, token, stub, host):
    host.runs = [
        run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"),
        run(R2, "2026-10-18T10:00:00Z", "2026-10-18T10:15:00Z", ticket="OTHER-5"),
    ]
    res = client.get(f"/api/w/{stub.ws}/usage", headers=hdr(token))
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["available"] and got["scope"] == "your" and got["tokens"] is None and got["cost"] is None
    week = got["windows"]["week"]
    assert (week["runs"], week["ms"]) == (2, 2700 * 1000)
    # OTHER-5 is not on this person's board: it is never named, even though the host listed the run
    assert [(r["ticket"], r["other"]) for r in week["tickets"]] == [("DEMO-1", False), (None, True)]
    assert "OTHER-5" not in json.dumps(week)
    host.runs = host.runs[:1]  # the host forgot R2; only the ledger remembers it
    again = client.get(f"/api/w/{stub.ws}/usage", headers=hdr(token)).json()["windows"]["week"]
    assert (again["runs"], again["ms"]) == (2, 2700 * 1000)
    assert [(r["ticket"], r["other"]) for r in again["tickets"]] == [("DEMO-1", False), (None, True)]
    assert "OTHER-5" not in json.dumps(again)
    assert any(c[0] == "run.list" and c[1] == {"state": "all", "kind": "agent"} for c in host.calls)
    assert not any(c[0] not in ("run.list", "ticket.query", "addon.list") for c in host.calls)  # reads only


def test_ticket_route_lists_the_recent_runs_of_that_ticket_only(client, token, stub, host):
    host.runs = [
        run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"),
        run(R2, "2026-10-18T09:00:00Z", "2026-10-18T09:15:00Z", ticket="OTHER-5"),
    ]
    got = client.get(f"/api/w/{stub.ws}/usage?ticket=DEMO-1", headers=hdr(token)).json()
    assert got["windows"]["week"]["runs"] == 1 and len(got["recent"]) == 1
    assert got["recent"][0] == {
        "started_at": "2026-10-18T10:00:00Z",
        "ended_at": "2026-10-18T10:30:00Z",
        "ms": 1800000,
        "running": False,
        "harness": "claude",
    }
    hidden = client.get(f"/api/w/{stub.ws}/usage?ticket=NOPE-1", headers=hdr(token)).json()
    assert hidden["windows"]["week"]["runs"] == 0 and hidden["recent"] == []


def test_route_refuses_while_the_add_on_is_off(client, token, stub, host):
    host.on = False
    res = client.get(f"/api/w/{stub.ws}/usage", headers=hdr(token))
    assert res.status_code >= 400 and res.json()["error"]["code"] == "addon.disabled"
    assert not any(c[0] == "run.list" for c in host.calls)


@pytest.mark.parametrize("query", ["ticket=../x", "ticket=DEMO-1&x=1", "window=all"])
def test_query_is_closed(client, token, stub, host, query):
    assert client.get(f"/api/w/{stub.ws}/usage?{query}", headers=hdr(token)).status_code == 400


@pytest.mark.parametrize(
    "doc", [{"v": 1, "people": {"p_me": 5}}, {"v": 1, "people": {"p_me": {"w1": []}}}, {"v": 1, "people": []}]
)
def test_malformed_ledger_entries_are_reset_not_a_500(tmp_path, doc):
    env = {"ORCH_STATE_DIR": str(tmp_path)}
    path = tmp_path / "dashboard" / usage.FILE
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(doc))
    path.chmod(0o600)
    fs = facts(run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"))
    assert list(usage.remember("p_me", "w1", fs, NOW, env)["runs"]) == [R1]


def test_ledger_with_open_permissions_or_a_link_is_not_trusted(tmp_path):
    env = {"ORCH_STATE_DIR": str(tmp_path)}
    fs = facts(run(R1, "2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"))
    usage.remember("p_me", "w1", fs, NOW, env)
    path = tmp_path / "dashboard" / usage.FILE
    path.chmod(0o644)
    assert usage.remember("p_me", "w1", [], NOW, env)["runs"] == {}  # unsafe mode: a fresh ledger
    other = tmp_path / "elsewhere.json"
    other.write_text(path.read_text())
    path.unlink()
    path.symlink_to(other)
    assert usage._read(path) == {"v": 1, "people": {}}
