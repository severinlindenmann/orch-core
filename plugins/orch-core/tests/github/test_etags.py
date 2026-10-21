from __future__ import annotations

from orch.github.etags import EtagStore


def test_partition_lru_and_size():
    store = EtagStore(max_entries=2, max_bytes=100)
    store.put("one", "/repos/a/b", '"a"', [1])
    assert store.get("two", "/repos/a/b") is None
    store.put("one", "b", '"b"', [2])
    assert store.get("one", "/repos/a/b") == ('"a"', [1])
    store.put("one", "c", '"c"', [3])
    assert store.get("one", "b") is None
    value = store.get("one", "c")
    value[1].append(4)
    assert store.get("one", "c")[1] == [3]
    store.put("one", "large", '"d"', "x" * 101)
    assert store.get("one", "large") is None
    store.put("two", "c", '"c"', {})
    store.drop_other_tokens("two")
    assert store.get("one", "c") is None
    store.clear()
    assert store.size_bytes == 0 and store.get("two", "c") is None
