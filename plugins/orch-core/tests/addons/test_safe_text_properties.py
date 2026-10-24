"""Property tests of the stderr redaction (G6 A1, Opus review A1-1): every cut, overlapping forms, escapes."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from orch.addons import runner as r


def pipeline(stream: bytes, secret: str, cap: int) -> str:
    slack = max(len(f.encode()) for f in r._forms(secret))  # noqa: SLF001
    return r._safe_text(stream[: cap + slack], secret, cap)  # noqa: SLF001  (_converse keeps cap + slack bytes)


FORMS = st.sampled_from(["S", "E"])


@settings(max_examples=800, deadline=None)
@given(
    secret=st.text(alphabet='QW\\"', min_size=1, max_size=12),
    parts=st.lists(st.one_of(st.text(alphabet="xyz\n[]é日", max_size=30), FORMS), max_size=40),
    cap=st.integers(min_value=0, max_value=300),
)
def test_whole_copies_leave_no_secret_letter(secret, parts, cap):
    forms = {"S": secret, "E": json.dumps(secret)[1:-1]}
    stream = "".join(forms.get(p, p) for p in parts).encode()
    out = pipeline(stream, secret, cap)
    rest = out.replace("[redacted]", "")
    assert not any(c in rest for c in "QW"), (secret, stream, cap, out)
    assert len(out.encode()) <= cap + 3 * 0 or out.endswith("�") or len(out.encode()) <= cap


@settings(max_examples=800, deadline=None)
@given(
    secret=st.text(alphabet='QW\\"', min_size=1, max_size=12),
    parts=st.lists(st.one_of(st.text(alphabet="xyz", max_size=30), FORMS), max_size=40),
    cap=st.integers(min_value=0, max_value=300),
)
def test_whole_copies_leave_no_escape_residue(secret, parts, cap):
    forms = {"S": secret, "E": json.dumps(secret)[1:-1]}
    stream = "".join(forms.get(p, p) for p in parts).encode()
    rest = pipeline(stream, secret, cap).replace("[redacted]", "")
    assert set(rest) - set("[redacted]") <= set("xyz"), (secret, stream, cap, rest)


@settings(max_examples=800, deadline=None)
@given(
    secret=st.text(alphabet='QW\\"', min_size=1, max_size=8),
    noise=st.text(alphabet='QW\\"x', max_size=200),
    cap=st.integers(min_value=0, max_value=150),
)
def test_no_whole_form_and_no_trailing_prefix(secret, noise, cap):
    out = pipeline(noise.encode(), secret, cap)
    for form in r._forms(secret):
        assert form not in out
    assert len(out.encode()) <= cap


@settings(max_examples=500, deadline=None)
@given(
    secret=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=40),
    pre=st.binary(max_size=50),
    cap=st.integers(min_value=0, max_value=120),
)
def test_printable_ascii_secret_cut_anywhere(secret, pre, cap):
    if secret in "[redacted]":
        return
    stream = pre + secret.encode() + pre
    out = pipeline(stream, secret, cap)
    assert secret not in out
    if len(secret) >= 4:
        assert not any(out.endswith(secret[:k]) for k in range(4, len(secret)))
