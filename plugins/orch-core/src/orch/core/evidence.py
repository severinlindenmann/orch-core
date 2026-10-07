"""Acceptance criteria and the evidence that proves them (ticket design review §1.3, §3.1).

Criteria are the top-level `- [ ]` / `- [x]` lines of `## Acceptance criteria`, numbered AC1…ACn in order (the same
numbering as task refs `ac:<n>`). A top-level line of `## Verification` that names `AC<n>` is evidence for that
criterion (`- AC2: curl returned 404 once (curl -i …)`; several: `- AC1, AC3: …`); indented lines and fenced blocks
under it belong to it. A criterion is proven only when it has evidence: a tick alone proves nothing, and ticking
one needs evidence first (`Ops.set_section`)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from orch.core import fences

_CRITERION = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_CITE = re.compile(r"(?<![A-Za-z0-9])AC\s?(\d+)(?![0-9])", re.IGNORECASE)
# "- AC1: …", "- AC1, AC3 — …": the citing prefix, left out of the evidence text
_PREFIX = re.compile(r"^(?:AC\s?\d+\s*(?:,|/|&|\+|and)?\s*)+[:—–-]\s*", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# Evidence must say something: a citing line whose text (with any indented output) has fewer than MIN_EVIDENCE
# letters or digits, or that is only a placeholder ("todo", "TBD: later", "n/a", "?", "…"), proves nothing.
MIN_EVIDENCE = 8
_PLACEHOLDER = re.compile(r"^(?:todo|to do|tbd|tba|wip|n/?a|none|pending|later|soon|fixme|xxx|tk)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Criterion:
    n: int
    text: str
    ticked: bool
    evidence: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return bool(self.evidence)


def _criteria_lines(text: str) -> list[tuple[str, bool]]:
    out = []
    for line in (text or "").split("\n"):
        m = None if line[:1].isspace() else _CRITERION.match(line.strip())
        if m:
            out.append((m.group(2).strip(), m.group(1) != " "))
    return out


def _blocks(text: str) -> list[str]:
    """Top-level lines of `text`, each with the indented lines and fenced blocks that follow it. Text before the
    first top-level line becomes a block of its own that starts indented (it never cites a criterion)."""
    blocks: list[list[str]] = []
    fence = None
    for line in (text or "").split("\n"):
        was = fence
        fence, is_fence = fences.step(fence, line)
        if was is not None:
            blocks[-1].append(line)
            continue
        if not line.strip():
            continue
        if blocks and (is_fence or line[:1].isspace()):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(b) for b in blocks]


def _cited(head: str) -> list[int]:
    return [] if head[:1].isspace() else [int(n) for n in _CITE.findall(head)]


def substantial(text: str) -> bool:
    """True when an evidence text says something (see MIN_EVIDENCE); placeholders and near-empty lines do not."""
    plain = text.strip()
    if _PLACEHOLDER.match(plain.lstrip("([*_`").strip()):
        return False
    return sum(ch.isalnum() for ch in plain) >= MIN_EVIDENCE


def _evidence_text(block: str) -> str:
    head, _, rest = block.partition("\n")
    head = _PREFIX.sub("", _BULLET.sub("", head.strip()))
    return head + ("\n" + rest if rest else "")


def criteria(ticket) -> list[Criterion]:
    found = _criteria_lines(ticket.section("Acceptance criteria"))
    proofs: dict[int, list[str]] = {}
    for block in _blocks(ticket.section("Verification")):
        head = block.split("\n", 1)[0]
        text = _evidence_text(block)
        if not substantial(text):
            continue
        for n in dict.fromkeys(_cited(head)):
            if 1 <= n <= len(found):
                proofs.setdefault(n, []).append(text)
    return [Criterion(i, text, ticked, tuple(proofs.get(i, ()))) for i, (text, ticked) in enumerate(found, 1)]


def other_evidence(ticket) -> list[str]:
    """Verification lines that cite no existing criterion (general evidence, shown apart)."""
    total = len(_criteria_lines(ticket.section("Acceptance criteria")))
    out = []
    for block in _blocks(ticket.section("Verification")):
        if not any(1 <= n <= total for n in _cited(block.split("\n", 1)[0])):
            out.append(_evidence_text(block))
    return out


def progress(ticket) -> tuple[int, int]:
    """(proven, total) criteria."""
    cs = criteria(ticket)
    return sum(1 for c in cs if c.proven), len(cs)


def missing(ticket) -> list[int]:
    """Numbers of the criteria without evidence."""
    return [c.n for c in criteria(ticket) if not c.proven]


def ticked_without_evidence(ticket, *, before=None) -> list[int]:
    """Criteria ticked in `ticket` that have no evidence. With `before` (the ticket as it was), only the ticks that
    are new, so an old file whose ticks predate this rule can still be edited."""
    was = {text for text, ticked in _criteria_lines(before.section("Acceptance criteria")) if ticked} if before else set()
    return [c.n for c in criteria(ticket) if c.ticked and not c.proven and c.text not in was]


# -- strict evidence: what an unattended close (orch.core.factory_close) relies on -------------------------------------
# The rules above are for a human who reads the evidence before signing. A close nobody reads needs more, and still
# proves nothing was run: it only refuses evidence that is plainly not evidence. For every criterion n:
# - a doubt word in ANY top-level Verification line (with its indented lines) that mentions AC n anywhere, prefix or
#   text, single or several criteria, blocks n: a passing line never outvotes a doubting one;
# - and at least one line cites exactly that criterion in its prefix only (`- AC2: ...`; a line citing several, or a
#   criterion named only inside the text, proves none), says something, and names something concrete: a command in
#   backticks, a test name (`test_x`, `name.test.ts`, `pytest path::name`), a URL, a path or file with a known
#   extension, or a number next to a unit (`12 passed`, `3 rows`, `40 ms`).
# A ticket with no criteria has nothing strict evidence could prove: it is missing.
# An artifact receipt is the strongest form: `- AC1: artifact <name>` (more text may follow) counts as concrete when
# <name> is a file `orch artifact add` recorded for this very ticket, its recorded sha256 matches the bytes on disk and
# the file is not empty; a line that claims an artifact that does not resolve so proves nothing, whatever else it says.
# Doubt words are not looked for inside backticks or inside hyphenated identifiers (`grep -rn TODO src`, the
# `unverified-verdict` finding). Limits, stated plainly: the list is fixed English, so other languages and other
# phrasing pass; a doubt word in plain prose blocks even when the sentence means the opposite ("returns 403 when access
# is blocked" blocks: put such text in backticks or rephrase); "concrete" is only a pattern; nothing checks the text is
# true.
_STRICT_PREFIX = re.compile(r"^AC\s?(\d+)\s*[:—–-]\s*", re.IGNORECASE)
_DOUBT = re.compile(
    r"\b(?:could\s*n[o']t|couldn't|can't|cannot|unable|unverified|untested|blocked|skip(?:ped|s)?|todo|to do|tbd|"
    r"fixme|wip|not\s+(?:verified|tested|checked|run|yet|done|possible|working|able|reproduced|implemented|applicable)|"
    r"did\s*n[o']t|didn't|was\s*n[o']t|wasn't|doesn't|does\s+not|isn't|won't|no\s+access|failed\s+to|n/a|"
    r"should\s+work|probably|maybe|assum(?:e|ed|ing)|fails|failing|pending|partially)(?![\w-])", re.IGNORECASE)
_CODE = re.compile(r"`[^`\n]+`")
_HYPHENATED = re.compile(r"\b\w+(?:-\w+)+\b")
_EXTENSIONS = ("py|pyi|js|mjs|cjs|ts|tsx|jsx|json|jsonl|md|yaml|yml|toml|txt|csv|tsv|html|css|scss|sh|go|rs|rb|java|"
               "kt|swift|c|h|cc|cpp|hpp|cs|php|sql|xml|ini|cfg|conf|lock|log|png|jpg|svg|pdf|proto|tf|vue|svelte")
_CONCRETE = re.compile(
    r"`[^`\n]+`"                                                  # a command or code in backticks
    r"|https?://\S+"                                              # a URL
    r"|\btest_\w+|\b[\w-]+\.test\.\w+\b|\bpytest\s+\S+::\w+"      # a test name
    rf"|\b[\w./-]*\w\.(?:{_EXTENSIONS})\b"                        # a path or file with a known extension
    r"|\b\d+\s*(?:passed|failed|tests?|checks?|ms|seconds?|secs?|lines?|files?|rows?|records?|bytes|kb|mb|errors?|"
    r"warnings?|items?|requests?|calls?|cases?|assertions?)\b", re.IGNORECASE)


def _doubt(text: str) -> str | None:
    """The first doubt word in `text`, outside backticks and hyphenated identifiers, or None."""
    m = _DOUBT.search(_HYPHENATED.sub(" ", _CODE.sub(" ", text)))
    return m.group(0) if m else None


_RECEIPT = re.compile(r"^artifact\s+`?([^\s`]+)`?", re.IGNORECASE)


def receipt_why(ws, ticket, name: str) -> str | None:
    """Why `artifact <name>` is not a receipt of `ticket`, or None when it is (see above)."""
    from orch.core import artifacts
    e = artifacts.find(ticket, name)
    sha = e.get("sha256") if e else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return f"artifact {name} is not a file orch recorded for {ticket.id} (`orch artifact add`)"
    p = artifacts.ticket_dir(ws, ticket.id) / name
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
            return f"artifact {name} is missing or empty"
    except OSError:
        return f"artifact {name} cannot be read"
    if artifacts.file_sha256(p) != sha:
        return f"artifact {name} changed since orch recorded it"
    return None


def strict_why(text: str, ws=None, ticket=None) -> str | None:
    """Why an evidence text does not meet the strict rules, or None when it does. A text that starts with
    `artifact <name>` is judged as a receipt: it must resolve against `ticket` in `ws` (receipt_why), and is then
    concrete; without them it proves nothing."""
    r = _RECEIPT.match(text.strip())
    if r:
        if ws is None or ticket is None:
            return "an artifact receipt can be checked only against its ticket"
        why = receipt_why(ws, ticket, r.group(1))
        if why:
            return why
        word = _doubt(text)
        return f"it says {word!r}" if word else None
    if not substantial(text):
        return "it says too little"
    word = _doubt(text)
    if word:
        return f"it says {word!r}"
    if not _CONCRETE.search(text):
        return ("it names nothing concrete (a command in backticks, a test name, a URL, a file with a known extension "
                "or a number with a unit)")
    return None


def strict_missing(ticket, ws=None) -> list[tuple[int, str]]:
    """[(criterion number, why)] for every criterion of `ticket` without strict evidence; [] when each has it. A ticket
    without criteria gives [(0, why)]. `ws` lets artifact receipts be checked (without it they prove nothing)."""
    total = len(_criteria_lines(ticket.section("Acceptance criteria")))
    if total == 0:
        return [(0, "the ticket has no acceptance criteria")]
    seen: dict[int, str] = {}
    doubted: dict[int, str] = {}
    ok: set[int] = set()
    for block in _blocks(ticket.section("Verification")):
        head, _, rest = block.partition("\n")
        if head[:1].isspace():
            continue
        line = _BULLET.sub("", head.strip())
        word = _doubt(block)
        if word:  # every criterion this line names is in doubt, whatever another line says
            for n in {int(x) for x in _CITE.findall(block)}:
                doubted.setdefault(n, f"a line about it says {word!r}")
        m = _STRICT_PREFIX.match(line)
        p = _PREFIX.match(line)
        if not m or p is None or p.group(0) != m.group(0):
            continue  # no prefix, or a prefix citing more than one criterion
        n = int(m.group(1))
        why = strict_why(line[m.end():] + ("\n" + rest if rest else ""), ws, ticket)
        if why is None:
            ok.add(n)
        else:
            seen.setdefault(n, why)
    return [(n, doubted.get(n) or seen.get(n, "no line cites it alone as `- AC%d: ...`" % n))
            for n in range(1, total + 1) if n in doubted or n not in ok]
