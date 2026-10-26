"""The recovery code is shown numbered; input accepts plain and numbered forms."""

import pytest

from orch.identity import recovery as r


def _code():
    return r.code_from_entropy(bytes(range(32)))


def test_numbered_display_is_four_rows_of_six_with_aligned_columns():
    code = _code()
    rows = r.format_code_numbered(code).split("\n")
    assert len(rows) == 4
    assert rows[0].lstrip().startswith("1 ") and " 6 " in rows[0] and rows[3].rstrip().split()[-2] == "24"
    assert [w for row in rows for w in row.split()[1::2]] == code.split()
    assert [int(n) for row in rows for n in row.split()[0::2]] == list(range(1, 25))
    starts = {
        tuple([i for i, ch in enumerate(row) if ch != " " and (i == 0 or row[i - 1] == " ")][1::2]) for row in rows[:3]
    }
    assert len(starts) == 1  # every full row has its columns at the same offsets


@pytest.mark.parametrize("sep", [" ", ": ", ". ", ") ", ":"])
def test_numbered_input_forms_normalise_to_plain(sep):
    code = _code()
    typed = "  ".join(f"{i + 1}{sep}{w}" for i, w in enumerate(code.split()))
    assert r.check_recovery_code(typed) == code
    assert r.check_recovery_code(r.format_code_numbered(code)) == code
    assert r.check_recovery_code(code) == code


def test_numbers_out_of_sequence_are_not_stripped():
    code = _code()
    typed = " ".join(f"{i + 2} {w}" for i, w in enumerate(code.split()))
    with pytest.raises(r.RecoveryCodeError):
        r.check_recovery_code(typed)
