"""The terminal's person label follows the dashboard's ``personLabel`` (SignContent.test.tsx): the same table."""

import pytest

from orch.host import personlabel

SEV = "p_181d953f12ab" + "0" * 18
A, B = "p_aaaa1111222233334444555566667777", "p_bbbb1111222233334444555566667777"
MEMBERS = [
    {"person": SEV, "name": "Severin"},
    {"person": A, "name": "Sam"},
    {"person": B, "name": "S​aｍ  "},
    {"person": "p_cccc1111222233334444555566667777", "name": "Mal (p_dddd1111222233334444555566667777)"},
    {"person": "p_eeee1111222233334444555566667777", "name": "Dev  Two"},
    {"person": "p_ffff1111222233334444555566667777", "name": "dev two"},
    {"person": "p_9999111122223333444455556666777a", "name": "hidden-name", "hidden": True},
    {"person": "p_7777111122223333444455556666777c", "name": "（p_dddd1111222233334444555566667777）"},
    {"person": "p_6666111122223333444455556666777d", "name": "Ѕamuel"},
    {"person": "p_5555111122223333444455556666777e", "name": "Sev\U0001f600"},
    {"person": "p_4444111122223333444455556666777f", "name": "Ann…"},
    {"person": "p_3333111122223333444455556666777a", "name": "Anna⃝"},
    {"person": "p_2222111122223333444455556666777b", "name": "Zó̂̃e"},
    {"person": "p_1111111122223333444455556666777c", "name": "Іvan"},
    {"person": "p_1212111122223333444455556666777c", "name": "Ivan"},
    {"person": "p_8888111122223333444455556666777b", "name": "א‮בג"},
]
TABLE = [
    (SEV, "Severin (p_181d953f12ab…)"),
    (A, "p_aaaa11112222… (name shared by 2 members)"),
    (B, "p_bbbb11112222… (name shared by 2 members)"),
    ("p_cccc1111222233334444555566667777", "p_cccc11112222… (name not shown)"),
    ("p_eeee1111222233334444555566667777", "p_eeee11112222… (name shared by 2 members)"),
    ("p_7777111122223333444455556666777c", "p_777711112222… (name not shown)"),
    ("p_6666111122223333444455556666777d", "p_666611112222… (name not shown)"),
    ("p_5555111122223333444455556666777e", "p_555511112222… (name not shown)"),
    ("p_4444111122223333444455556666777f", "p_444411112222… (name not shown)"),
    ("p_3333111122223333444455556666777a", "p_333311112222… (name not shown)"),
    ("p_2222111122223333444455556666777b", "p_222211112222… (name not shown)"),
    ("p_1111111122223333444455556666777c", "p_111111112222… (name shared by 2 members)"),
    ("p_9999111122223333444455556666777a", "p_999911112222… (name not shown)"),
    ("p_0badc0de99887766554433221100ffee", "p_0badc0de9988… (unknown person)"),
    ("p_181d953f…", "p_181d953f… (unknown person)"),
    (
        "p_8888111122223333444455556666777b",
        "\u05d0\u05d1\u05d2 (p_888811112222…)",
    ),  # the bidi control is dropped, the id stays its own isolate
]


@pytest.mark.parametrize(("value", "want"), TABLE)
def test_the_dashboard_table(value, want):
    assert personlabel.label(value, MEMBERS) == want


def test_lookalikes_across_scripts_count_as_shared():
    cyr = [{"person": A, "name": "Ѕеѵегіn"}, {"person": B, "name": "Severin"}]
    assert personlabel.label(A, cyr) == "p_aaaa11112222… (name shared by 2 members)"
    greek = [{"person": A, "name": "ΝΙΚΟ"}, {"person": B, "name": "NIKO"}]
    assert personlabel.label(B, greek) == "p_bbbb11112222… (name shared by 2 members)"


@pytest.mark.parametrize("name", ["Sevא", "Sevㅤerin", "Sevᅟerin", "Sevﾠerin", "x" * 41, "", "A1"])
def test_names_the_allowlist_refuses(name):
    assert personlabel.label(SEV, [{"person": SEV, "name": name}]) == "p_181d953f12ab… (name not shown)"


def test_not_a_person_id():
    assert personlabel.label("owner", MEMBERS) is None and personlabel.label(None, MEMBERS) is None
    assert personlabel.label("p_xyz", MEMBERS) is None


def test_the_terminal_marks_a_shown_name_as_the_hosts_word_and_keeps_the_id():
    got = personlabel.label(SEV, MEMBERS, unsigned_name=True)
    assert got == "Severin (p_181d953f12ab…; name per host, not signed)"
    for value, want in TABLE[1:]:  # every other row is id-only and reads as the dashboard's
        if "(" in want and "name per host" not in want and want.startswith("p_"):
            assert personlabel.label(value, MEMBERS, unsigned_name=True) == want


def test_a_sharp_s_twin_is_shared_like_the_dashboard():
    twins = [{"person": A, "name": "Stra\u00dfe"}, {"person": B, "name": "Strasse"}]
    assert personlabel.label(A, twins) == "p_aaaa11112222… (name shared by 2 members)"


def test_a_combining_mark_that_case_folds_to_a_letter_does_not_survive_as_one():
    # U+0345 (combining ypogegrammeni) upper-cases to Greek capital iota: "Ann\u0345" must skeleton like "Anni"
    twins = [{"person": A, "name": "Ann\u0345"}, {"person": B, "name": "Anni"}]
    assert personlabel.label(A, twins) == "p_aaaa11112222… (name shared by 2 members)"
