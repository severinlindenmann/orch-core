"""The poll bookkeeping of add-on events (#718): intervals, backoff, budgets, fairness, failed sources. T0:
``uv run pytest tests/host/test_addonpoll.py -x -q``"""

from orch.host import addonpoll as ap


def it(addon="dbx", ref="1", interval=300):
    return (addon, f"{addon}.run.finished", ref, interval)


def test_nothing_waiting_means_nothing_to_poll():
    assert ap.Poller().plan([], 0.0, 0.0) == []


def test_a_new_ref_is_polled_at_once_then_at_its_interval():
    p = ap.Poller()
    x = it()
    assert p.plan([x], 100.0, 1000.0) == [x]
    p.pending(x, 100.0)
    assert p.plan([x], 399.0, 1299.0) == [] and p.plan([x], 400.0, 1300.0) == [x]


def test_the_interval_is_bounded():
    p = ap.Poller()
    fast, slow = it(ref="1", interval=1), it(ref="2", interval=10**6)
    p.pending(fast, 0.0)
    p.pending(slow, 0.0)
    assert p.plan([fast, slow], 59.0, 59.0) == []
    assert p.plan([fast, slow], 60.0, 60.0) == [fast] and p.plan([slow], 3599.0, 3599.0) == []
    assert p.plan([slow], 3600.0, 3600.0) == [slow]


def test_many_follow_ups_on_one_ref_make_one_call():
    p = ap.Poller()
    assert p.plan([it(), it(), it()], 0.0, 0.0) == [it()]


def test_failures_back_off_and_the_third_marks_the_source_failed():
    p = ap.Poller()
    x = it(interval=100)
    p.failed(x, "timeout", 0.0, 10.0)
    assert p.plan([x], 199.0, 0.0) == [] and p.plan([x], 200.0, 0.0) == [x]  # 100 s doubled
    p.failed(x, "timeout", 200.0, 20.0)
    assert p.source(x[1], x[2]) is None
    p.failed(x, "crashed", 600.0, 30.0)
    s = p.source(x[1], x[2])
    assert s["state"] == "failed" and s["code"] == "crashed" and s["since"] == 30.0
    assert p.plan([x], 1399.0, 0.0) == [] and p.plan([x], 1400.0, 0.0) == [x]  # 100 s, doubled three times
    for _ in range(5):
        p.failed(x, "timeout", 10000.0, 40.0)
    assert p.plan([x], 13599.0, 0.0) == [] and p.plan([x], 13600.0, 0.0) == [x]  # capped at an hour
    p.pending(x, 5000.0)  # one good answer clears it
    assert p.source(x[1], x[2]) is None


def test_the_failure_code_is_a_closed_word():
    p = ap.Poller()
    x = it()
    for i in range(3):
        p.failed(x, "<script>", float(i), float(i))
    assert p.source(x[1], x[2])["code"] == "other"
    assert ap.code_of("addon.timeout") == "timeout" and ap.code_of("anything") == "other"


def test_the_budgets_cap_calls_per_addon_per_host_and_per_tick():
    p = ap.Poller(addon_hour=3, host_hour=5, per_tick=2)
    a = [it("a", str(i)) for i in range(4)]
    b = [it("b", str(i)) for i in range(4)]
    first = p.plan(a + b, 0.0, 0.0)
    assert len([x for x in first if x[0] == "a"]) == 2 and len([x for x in first if x[0] == "b"]) == 2
    for x in first:
        p.pending(x, 0.0)
    second = p.plan(a + b, 301.0, 301.0)  # host: 4 of 5 used, one call left
    assert len(second) == 1
    for x in second:
        p.pending(x, 301.0)
    assert p.plan(a + b, 700.0, 700.0) == []  # host: 5 of 5 used (the 6th would exceed)
    assert p.plan(a + b, 4000.0, 3601.0) != []  # an hour later the count is gone


def test_a_ref_over_budget_goes_first_next_time_so_none_starves():
    p = ap.Poller(per_tick=1)
    xs = [it("a", str(i)) for i in range(3)]
    order = []
    for t in range(3):
        got = p.plan(xs, 1000.0 * (t + 1), 1000.0 * (t + 1))
        assert len(got) == 1
        order.append(got[0][2])
        p.pending(got[0], 1000.0 * (t + 1))
    assert sorted(order) == ["0", "1", "2"]


def test_forget_drops_what_no_follow_up_waits_on_and_state_persists():
    saved = {}
    p = ap.Poller(save=lambda d: saved.update(d))
    x, y = it(ref="1"), it(ref="2")
    for _ in range(3):
        p.failed(x, "timeout", 0.0, 5.0)
    p.plan([x, y], 0.0, 5.0)
    p.forget([y])
    assert p.source(x[1], x[2]) is None
    q = ap.Poller(
        saved={
            "calls": saved["calls"],
            "sources": {"bad": {"code": "x"}, "dbx.run.finished|2": {"code": "crashed", "since": 1}},
        }
    )
    assert list(q.sources) == ["dbx.run.finished|2"] and len(q.calls) == 1
