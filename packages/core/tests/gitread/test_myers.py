"""The Myers line diff (:mod:`orch.gitread.myers`): the core on small inputs, and seeded sweeps that apply the hunks."""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.gitread import myers
from orch.gitread.myers import diff_flags, hunks, render, split_lines


def apply(a: list[bytes], hs: list[myers.Hunk]) -> list[bytes]:
    """Apply hunks to the old lines (checking context and deletions on the way)."""
    out: list[bytes] = []
    pos = 0
    for h in hs:
        assert h.a_start >= pos
        out += a[pos : h.a_start]
        pos = h.a_start
        for op, rec in h.lines:
            if op == "+":
                out.append(rec)
            else:
                assert a[pos] == rec
                pos += 1
                if op == " ":
                    out.append(rec)
        assert pos == h.a_start + h.a_len
    return out + a[pos:]


def lines(*xs: str) -> list[bytes]:
    return [f"{x}\n".encode() for x in xs]


def edit_count(a, b):
    da, ib = diff_flags(a, b)
    return sum(da) + sum(ib)


def test_split_lines_keeps_newlines_and_an_unterminated_last_line():
    assert split_lines(b"") == []
    assert split_lines(b"a\nb") == [b"a\n", b"b"]
    assert split_lines(b"a\r\nb\n") == [b"a\r\n", b"b\n"]
    assert split_lines(b"\n\n") == [b"\n", b"\n"]


def test_empty_inputs():
    assert hunks([], []) == []
    (h,) = hunks([], lines("a", "b"))
    assert (h.a_start, h.a_len, h.b_start, h.b_len) == (0, 0, 0, 2)
    (h,) = hunks(lines("a"), [])
    assert (h.a_start, h.a_len, h.b_start, h.b_len) == (0, 1, 0, 0)
    assert render([], lines("x"))[0] == b"@@ -0,0 +1 @@\n+x\n"
    assert render(lines("x"), [])[0] == b"@@ -1 +0,0 @@\n-x\n"


def test_identical_inputs_have_no_hunks():
    a = lines(*"abcdef")
    assert hunks(a, list(a)) == []
    assert edit_count(a, list(a)) == 0


def test_all_replace_is_deletions_then_insertions():
    a, b = lines("1", "2", "3"), lines("x", "y")
    (h,) = hunks(a, b, 3)
    assert [op for op, _ in h.lines] == ["-", "-", "-", "+", "+"]
    assert edit_count(a, b) == 5


def test_long_common_prefix_and_suffix_leave_a_small_hunk():
    mid_a, mid_b = lines("old"), lines("new1", "new2")
    pre, suf = lines(*[f"p{i}" for i in range(5000)]), lines(*[f"s{i}" for i in range(5000)])
    a, b = pre + mid_a + suf, pre + mid_b + suf
    (h,) = hunks(a, b, 2)
    assert (h.a_start, h.a_len, h.b_len) == (4998, 5, 6)
    assert apply(a, [h]) == b


def test_context_merges_hunks_that_are_closer_than_twice_the_context():
    a = lines(*[str(i) for i in range(20)])
    b = list(a)
    b[5], b[12] = b"X\n", b"Y\n"  # six unchanged lines between the two changes
    assert len(hunks(a, b, 3)) == 1
    assert len(hunks(a, b, 2)) == 2
    assert [(h.a_start, h.a_len) for h in hunks(a, b, 0)] == [(5, 1), (12, 1)]


def test_a_missing_final_newline_is_marked():
    assert (
        render([b"a\n", b"b"], [b"a\n", b"c"])[0]
        == b"@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n"
    )


def test_function_name_of_the_hunk_header():
    a = ["int f(void)", "{", *[f"    x({i});" for i in range(20)], "}"]
    b = list(a)
    b[15] = "    y;"
    text = render(lines(*a), lines(*b), 1)[0]
    assert text.startswith(b"@@ -15,3 +15,3 @@ int f(void)\n")


def test_the_search_budget_gives_a_valid_if_larger_script(monkeypatch):
    monkeypatch.setattr(myers, "_BUDGET", 50)
    rnd = random.Random(7)
    a = lines(*[str(rnd.randrange(50)) for _ in range(300)])
    b = lines(*[str(rnd.randrange(50)) for _ in range(300)])
    assert apply(a, hunks(a, b, 3)) == b


def _check(a: list[bytes], b: list[bytes], ctx: int) -> None:
    assert apply(a, hunks(a, b, ctx)) == b


def test_seeded_random_sequences_reproduce_the_target():
    rnd = random.Random(20260101)
    for _ in range(300):
        alpha = rnd.choice([2, 3, 8, 40])
        a = lines(*[str(rnd.randrange(alpha)) for _ in range(rnd.randrange(0, 40))])
        b = lines(*[str(rnd.randrange(alpha)) for _ in range(rnd.randrange(0, 40))])
        _check(a, b, rnd.randrange(0, 5))


def test_the_script_is_shortest_on_known_cases():
    # the example of the paper: ABCABBA -> CBABAC has an edit distance of 5
    a, b = list(b"ABCABBA"), list(b"CBABAC")
    assert edit_count([bytes([x]) for x in a], [bytes([x]) for x in b]) == 5


@pytest.mark.slow
@settings(max_examples=400, deadline=None, derandomize=True)
@given(
    st.lists(st.integers(0, 6), max_size=60),
    st.lists(st.integers(0, 6), max_size=60),
    st.integers(0, 6),
)
def test_property_hunks_reproduce_the_target_and_are_shortest(xs, ys, ctx):
    a, b = [f"{x}\n".encode() for x in xs], [f"{y}\n".encode() for y in ys]
    _check(a, b, ctx)
    # shortest: n + m - 2 * LCS
    n, m = len(a), len(b)
    t = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            t[i][j] = t[i + 1][j + 1] + 1 if a[i] == b[j] else max(t[i + 1][j], t[i][j + 1])
    assert edit_count(a, b) == n + m - 2 * t[0][0]


@pytest.mark.slow
def test_seeded_large_sweep():
    rnd = random.Random(99)
    for _ in range(40):
        base = [str(rnd.randrange(30)) for _ in range(rnd.randrange(200, 600))]
        new = list(base)
        for _ in range(rnd.randrange(1, 40)):
            op = rnd.randrange(3)
            i = rnd.randrange(len(new) + 1)
            if op == 0:
                new.insert(i, str(rnd.randrange(30)))
            elif op == 1 and new:
                del new[min(i, len(new) - 1)]
            elif new:
                new[min(i, len(new) - 1)] = str(rnd.randrange(30))
        _check(lines(*base), lines(*new), rnd.randrange(0, 5))
