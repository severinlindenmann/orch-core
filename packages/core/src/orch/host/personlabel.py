"""A person id as a person reads it: the terminal side of the dashboard's ``personLabel``
(``packages/addons/dashboard/src/components/sign/SignContent.tsx``). The rules are the same on purpose and
``tests/host/test_personlabel.py`` replays the dashboard's table:

* the 12-hex id is the identity, a name is only a hint and is shown only when it is unambiguous;
* a name appears only for an EXACT full id of a member of THE request's workspace, only when it is unique after
  normalisation (NFKC, casefold, look-alike letters of other scripts mapped to Latin, combining marks dropped), when
  it passes an ALLOWLIST (letters and marks plus space . ' -, at most 40 characters, one look-alike script, no
  enclosing mark, no run of 3 combining marks, no invisible character) and the member did not ask to be hidden;
* it always comes with the short id (``p_`` and 12 hex); everything else shows the short id and says why there is no
  name. The full id is in the details of the review (``d``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["label", "showable_name"]

PERSON = re.compile(r"p_[0-9a-f]+(…|\.\.\.)?\Z")
_CONTROLS = re.compile("[​-‏‪-‮⁦-⁩]")
_LOOKALIKES: dict[str, str] = {}
for _latin, _many in {
    "a": "аα", "b": "вβ", "c": "сϲ", "d": "ԁ", "e": "еε", "h": "нһ", "i": "іιlӏ", "j": "јϳ", "k": "кκ", "m": "мμ",
    "n": "η", "o": "оο", "p": "рρ", "q": "ԛ", "r": "г", "s": "ѕ", "t": "тτ", "u": "υ", "v": "νѵ", "w": "ԝω",
    "x": "хχ", "y": "уүγ", "z": "ζ",
    "A": "АΑ", "B": "ВΒ", "C": "С", "E": "ЕΕ", "H": "НΗ", "I": "ІΙ", "J": "Ј", "K": "КΚ", "M": "МΜ", "N": "Ν",
    "O": "ОΟ", "P": "РΡ", "S": "Ѕ", "T": "ТΤ", "X": "ХΧ", "Y": "ΥҮ", "Z": "Ζ",
}.items():  # fmt: skip
    for _c in _many:
        _LOOKALIKES[_c] = _latin
_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "COPTIC")
_IGNORABLE = (  # Default_Ignorable_Code_Point that are not format characters (Cf)
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0x1BCA0, 0x1BCA3), (0xE0000, 0xE0FFF),
)  # fmt: skip


def _clean(name: str) -> str:
    return _CONTROLS.sub("", name)


def _canon(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", _clean(name)).strip())


def _map(text: str) -> str:
    return "".join(_LOOKALIKES.get(c, c) for c in text)


def _key(name: str) -> str:
    """The skeleton two names are compared by. Look-alikes are mapped on the original-case text (Greek capital Ν is N,
    small ν is v), then case is folded, then NFD drops the marks, then look-alikes are mapped AGAIN: a mark whose case
    mapping is a letter (U+0345 becomes ι) is a letter only after folding, and must not survive as one. The dashboard's
    ``nameKey`` does the same steps in the same order."""
    folded = _map(_canon(name)).lower().upper().lower()
    bare = "".join(c for c in unicodedata.normalize("NFD", folded) if not unicodedata.category(c).startswith("M"))
    return _map(bare).lower()


def _cat(c: str) -> str:
    return unicodedata.category(c)


def _script(c: str) -> str | None:
    try:
        first = unicodedata.name(c).split(" ", 1)[0]
    except ValueError:
        return None
    return first if first in _SCRIPTS else None


def _ignorable(c: str) -> bool:
    o = ord(c)
    return _cat(c) in ("Cf", "Cc", "Cs") or any(lo <= o <= hi for lo, hi in _IGNORABLE)


def showable_name(name: str) -> str | None:
    """``name`` as it may be shown, or ``None`` (same allowlist as the dashboard)."""
    n = _canon(name)
    if not n or len(n) > 40 or any(_ignorable(c) for c in n):
        return None
    if _cat(n[0])[0] not in "LM" or any(_cat(c)[0] not in "LM" and c not in " .'-" for c in n):
        return None
    if re.search(r"\.{2,}", n) or any(_cat(c) == "Me" for c in n):
        return None
    run = 0
    for c in unicodedata.normalize("NFD", n):
        run = run + 1 if _cat(c)[0] == "M" else 0
        if run >= 3:
            return None
    letters = [c for c in n if _cat(c)[0] == "L"]

    def covered(c: str) -> bool:
        return _script(c) == "LATIN" or c in _LOOKALIKES

    if any(covered(c) for c in letters) and not all(covered(c) for c in letters):
        return None  # covered letters mixed with letters the table does not cover: not judgeable
    scripts = {s for c in n if (s := _script(c)) is not None}
    return None if len(scripts) > 1 else n


def short_id(person: str) -> str:
    """``p_`` plus 12 hex characters: long enough to tell people apart, never the whole id."""
    return person if len(person) <= 14 else person[:14] + "…"


def label(value: Any, members: Iterable[Mapping[str, Any]], *, unsigned_name: bool = False) -> str | None:
    """What the plain view shows for a person id (``Severin (p_181d953f12ab…)``, ``p_181d953f12ab… (unknown person)``,
    ``… (name not shown)``, ``… (name shared by N members)``), or ``None`` when ``value`` is not a person id.
    ``members`` are the members of THE request's workspace: dicts with ``person``, ``name``, optionally ``hidden``.
    ``unsigned_name`` (the terminal) marks a shown name as the host's word:
    ``Severin (p_…; name per host, not signed)``.
    The short id is ALWAYS in the label: a name never replaces or hides it."""
    if not isinstance(value, str) or not PERSON.match(value):
        return None
    cut = value.endswith(("…", "..."))
    short = value if cut else short_id(value)
    people = list(members)
    hit = None if cut else next((m for m in people if m.get("person") == value), None)
    if hit is None:
        return f"{short} (unknown person)"
    if hit.get("hidden") or not isinstance(hit.get("name"), str):
        return f"{short} (name not shown)"
    key = _key(hit["name"])
    sharing = {
        m["person"] for m in people if not m.get("hidden") and isinstance(m.get("name"), str) and _key(m["name"]) == key
    }
    if len(sharing) > 1:
        return f"{short} (name shared by {len(sharing)} members)"
    name = showable_name(hit["name"])
    if name is None:
        return f"{short} (name not shown)"
    return f"{name} ({short}; name per host, not signed)" if unsigned_name else f"{name} ({short})"
