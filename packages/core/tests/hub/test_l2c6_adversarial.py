"""l2-c6 (Opus security review of core#1022): adversarial checks of the C6 share path against the authoritative hub
contract (orch-hub 29bf048, protocol 2 "Unicode" and 8.5: the text rule is evaluated under Unicode 16.0.0 and also
refuses Cs, Co, Cn and Default_Ignorable_Code_Point). The PR's vectors come from 5c04a8e, which predates that rule.

Expected on a1217fbc: the two ``text_ok`` tests FAIL (finding C6-1), the host-path test PASSES (the titles the host
builds are already escaped by ``orch.canon.text.clean``, so the gap is in the pre-sign guard, not yet reachable)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orch.canon.text import is_assigned, is_format
from orch.hubclient import sharefmt as F
from orch.hubclient import shares as S

# Default_Ignorable_Code_Point, Unicode 16.0 (same table as orch.identity.pairing._IGNORABLE)
_DI = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)  # fmt: skip


def _refused_29bf048(c: str) -> bool:
    cp = ord(c)
    return (
        cp < 0x20 or 0x7F <= cp <= 0x9F  # Cc
        or is_format(cp)  # Cf
        or cp in (0x2028, 0x2029)  # Zl, Zp
        or 0xD800 <= cp <= 0xDFFF  # Cs
        or 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD  # Co
        or not is_assigned(cp)  # Cn (Unicode 16.0, bundled)
        or any(a <= cp <= b for a, b in _DI)
    )  # fmt: skip


# the code points of the new share_item_title_* refusal vectors at 29bf048
NEW_REFUSED = {
    "private_use_uf8ff": "",
    "unassigned_u0378": "͸",
    "hangul_filler_u3164": "ㅤ",
    "hangul_filler_u115f": "ᅟ",
    "halfwidth_filler_uffa0": "ﾠ",
    "cgj_u034f": "͏",
    "variation_selector_ufe0f": "️",
}


@pytest.mark.parametrize("name", sorted(NEW_REFUSED))
def test_c6_1_text_ok_refuses_what_the_authoritative_contract_refuses(name):
    """C6-1: core's pre-sign guard must refuse what the hub (29bf048) refuses, or core signs and sends an item the hub
    answers with ``malformed`` (and ``sync`` stops at that item)."""
    assert not F.text_ok(f"a{NEW_REFUSED[name]}b", 200), f"text_ok accepts U+{ord(NEW_REFUSED[name]):04X}"


def test_c6_1_sharefmt_does_not_judge_text_with_the_runtime_unicode_tables():
    """C6-1: the contract pins Unicode 16.0.0; ``unicodedata`` follows the Python build (3.15 ships 17.0)."""
    src = (Path(F.__file__)).read_text(encoding="utf-8")
    assert "import unicodedata" not in src


SPOOF = "‮⁦​ㅤᅟﾠ͏️­ \x1b"


@pytest.mark.parametrize("ch", list(SPOOF))
def test_c6_2_host_built_titles_and_texts_pass_the_authoritative_rule(ch):
    """L2P4-4 (contract carry-over: the host escapes or skips, never sends a refused character). Agent-written ticket
    titles and question texts go through ``clean`` before they reach ``check_content``."""
    q = S.QuestionFact("Q1", f"pay {ch} bank\nline two {ch}", True)
    t = S.TicketFact(
        "01J9ZP0000000000000000000A", "K-1", "bug", f"Fix {ch} now", "open", ("x",), None, (q,),
        (S.NeedFact("question", "Q1", True), S.NeedFact("approve", None, True)),
    )  # fmt: skip
    items = S.select([{"kind": "type", "type": "bug"}, {"kind": "questions"}, {"kind": "needs_you"}], [t])
    assert {i.kind for i in items} == {"ticket", "question", "summary"}
    for i in items:
        for k in ("title", "text"):
            if k in i.payload:
                bad = [f"U+{ord(c):04X}" for c in i.payload[k] if c != "\n" and _refused_29bf048(c)]
                assert not bad, (i.logical, k, bad)


def _qticket(text):
    q = S.QuestionFact("Q1", text, True, "sha256:" + "a" * 64)
    return S.TicketFact(
        "01J9ZP0000000000000000000A", "K-1", "bug", "Ticket title", "open", (), None, (q,),
        (S.NeedFact("question", "Q1", True),), (), 1,
    )  # fmt: skip


def test_c6_3_a_needs_you_question_summary_never_carries_f1_questions_and_full_text_is_a_question_item():
    """C6-3 / C6bd-2 (V3-D31): the one-line summary of a question need is never answerable (no f1.questions); the full
    text (up to 2000 bytes, multi-line) travels in a `question` item that carries f1.questions[Q]."""
    text = "line one\n" + "x" * 1500 + "\nlast line"
    items = {i.kind: i for i in S.select(list(S.DEFAULT_RULES), [_qticket(text)])}
    summ, qi = items["summary"], items["question"]
    assert summ.f1 is None
    assert len(summ.payload["title"].encode()) <= 200 and "\n" not in summ.payload["title"]
    assert qi.payload["text"] == text
    assert set(qi.f1["questions"]) == {"Q1"}


def test_c6_3_b_question_over_2000_bytes_is_not_answerable():
    items = S.select(list(S.DEFAULT_RULES), [_qticket("y" * 2001)])
    assert [i.kind for i in items] == ["summary"] and items[0].f1 is None
