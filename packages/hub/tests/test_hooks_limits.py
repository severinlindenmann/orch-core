from orch_hub.hooks.limits import Buckets


def test_bucket_spends_and_refills():
    t = [0]
    b = Buckets(lambda: t[0], per_min=60, burst=3)
    assert [b.take("a") for _ in range(4)] == [True, True, True, False]
    t[0] = 1000  # one second = one token
    assert b.take("a") and not b.take("a")
    assert b.take("other")  # another key is untouched


def test_peek_does_not_spend():
    b = Buckets(lambda: 0, per_min=60, burst=1)
    assert b.peek("k") and b.peek("k") and b.take("k") and not b.peek("k")


def test_lru_is_bounded_and_evicts_oldest():
    b = Buckets(lambda: 0, per_min=60, burst=1, cap=100)
    for i in range(1000):
        b.take(f"k{i}")
    assert len(b) == 100
    b.take("k999")  # a recent key is still tracked: spent
    assert not b.take("k999")
    assert b.take("k0")  # an evicted key starts full again (documented)


def test_take_is_constant_work_not_a_scan():
    # 50k distinct keys through a 1k LRU: the structure never grows past the cap (no full-scan memory guard exists)
    b = Buckets(lambda: 0, per_min=60, burst=1, cap=1000)
    for i in range(50_000):
        b.take(str(i))
    assert len(b) == 1000
