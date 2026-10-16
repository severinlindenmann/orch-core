"""The CI split (tests/_shard.py): every module lands in exactly one slice, and a bad spec fails loudly."""

from __future__ import annotations

import pytest

from tests._shard import FALLBACK_SECONDS_PER_TEST, WEIGHTS_FILE, assign, load_weights, module_weights, parse


def test_every_module_lands_in_exactly_one_slice():
    weights = {f"tests/m{i}.py": (i * 37) % 11 + 1 for i in range(40)}
    out = assign(weights, 3)
    assert set(out) == set(weights) and set(out.values()) <= {1, 2, 3}


def test_the_split_is_the_same_for_the_same_input_in_any_order():
    weights = {"a.py": 5, "b.py": 5, "c.py": 3, "d.py": 1}
    assert assign(weights, 2) == assign(dict(reversed(list(weights.items()))), 2)


def test_slices_are_balanced_by_test_count():
    weights = {f"m{i}.py": w for i, w in enumerate([50, 40, 30, 20, 10, 10, 5, 5])}
    out = assign(weights, 3)
    totals = [sum(w for m, w in weights.items() if out[m] == s) for s in (1, 2, 3)]
    assert max(totals) - min(totals) <= max(weights.values())


def test_a_single_slice_takes_everything():
    assert set(assign({"a.py": 1, "b.py": 2}, 1).values()) == {1}


@pytest.mark.parametrize("spec", ["", "3", "0/3", "4/3", "a/b", "1/2/3"])
def test_a_bad_spec_is_an_error(spec):
    with pytest.raises(ValueError):
        parse(spec)


def test_a_good_spec_parses():
    assert parse("2/3") == (2, 3)


def test_a_module_the_weights_do_not_know_is_weighted_by_its_test_count():
    out = module_weights({"known.py": 10, "new.py": 20}, {"known.py": 2.5})
    assert out == {"known.py": 2.5, "new.py": 20 * FALLBACK_SECONDS_PER_TEST}


def test_a_missing_weights_file_means_no_weights(tmp_path):
    assert load_weights(tmp_path / "none.json") == {}


def test_the_checked_in_weights_are_a_module_to_seconds_table():
    table = load_weights()
    assert table and all(m.startswith("tests/") and m.endswith(".py") and s >= 0 for m, s in table.items())
    assert WEIGHTS_FILE.name == "_shard_weights.json"
