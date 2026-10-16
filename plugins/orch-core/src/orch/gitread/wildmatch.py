"""gitignore globs without backtracking (#583, Opus M1 and L1 on #565).

The semantics are git's ``wildmatch`` with ``WM_PATHNAME``, as ``.gitignore`` uses it (``git help gitignore``,
``wildmatch.c``):

* ``*`` matches any run of bytes without ``/``; ``?`` one byte other than ``/``;
* ``**`` between slashes (or at the start before ``/``) matches zero or more whole directories; ``/**`` at the end and
  a lone ``**`` match everything below; any other run of stars is a plain ``*``;
* ``[...]``: a set (``!`` or ``^`` negates; ``]`` first is literal; ``a-z`` ranges; ``\\`` escapes; the twelve POSIX
  classes ``[:alpha:]`` ...), never matching ``/``. An unclosed ``[``, an unknown ``[:class:]``, or a ``\\`` at the
  end of the pattern makes the pattern match nothing, as in git (``WM_ABORT_ALL``);
* ``\\x`` is the literal byte ``x``.

**How.** The pattern is compiled once into tokens and matched by a set-of-states simulation (Thompson's
construction): every byte of the path advances all live states at once, so a match costs at most
``len(path) * len(tokens)`` steps, whatever the pattern. The regex translation this replaces backtracked: one
17-byte line ``*a*a*a*a*a*a*a*ab`` against a 60-byte name took 21 s (git: 0.02 s). A :class:`Budget` bounds the
total work of one tree check in direct mode, which has no child process wall clock: past it, the answer is unknown.
"""

from __future__ import annotations

import time

from .errors import Unavailable

__all__ = ["Budget", "Glob", "compile_glob"]

_LIT, _ONE, _CLS, _STAR, _DSTAR, _DIRS = range(6)
_SLASH = 0x2F


def _cc(name: bytes, c: int) -> bool | None:
    """git's ``[:name:]`` classes over ASCII (``ctype`` in the C locale); ``None``: an unknown class."""
    ch = chr(c)
    asc = c < 0x80
    tests = {
        b"alnum": asc and ch.isalnum(),
        b"alpha": asc and ch.isalpha(),
        b"blank": ch in " \t",
        b"cntrl": c < 0x20 or c == 0x7F,
        b"digit": asc and ch.isdigit(),
        b"graph": 0x21 <= c <= 0x7E,
        b"lower": asc and ch.islower(),
        b"print": 0x20 <= c <= 0x7E,
        b"punct": 0x21 <= c <= 0x7E and not ch.isalnum(),
        b"space": ch in " \t\n\r\v\f",
        b"upper": asc and ch.isupper(),
        b"xdigit": ch in "0123456789abcdefABCDEF",
    }
    return tests.get(name)


def _class(pat: bytes, i: int) -> tuple[bytes, int] | None:
    """The set starting at ``pat[i] == "["``, as git's ``dowild`` reads it: ``(256-byte table, index after "]")``, or
    ``None`` when git aborts (an unclosed set, a ``\\`` at the end, an unknown class)."""
    n = len(pat)
    p = i + 1
    negated = p < n and pat[p] in b"!^"
    if negated:
        p += 1
    items: list[tuple[str, int, int] | tuple[str, bytes]] = []
    prev = 0
    first = True
    while True:
        if p >= n:
            return None  # unclosed: git matches nothing
        ch = pat[p]
        if ch == 0x5D and not first:  # "]"
            break
        first = False
        if ch == 0x5C:  # "\": the next byte, literally
            p += 1
            if p >= n:
                return None
            ch = pat[p]
            items.append(("r", ch, ch))
            prev = ch
        elif ch == 0x2D and prev and p + 1 < n and pat[p + 1] != 0x5D:  # "-": a range from prev
            p += 1
            hi = pat[p]
            if hi == 0x5C:
                p += 1
                if p >= n:
                    return None
                hi = pat[p]
            items.append(("r", prev, hi))
            prev = 0
        elif ch == 0x5B and p + 1 < n and pat[p + 1] == 0x3A:  # "[:"
            end = pat.find(b"]", p + 2)
            if end < 0:
                return None
            if end - 1 >= p + 2 and pat[end - 1] == 0x3A:  # "[:name:]"
                name = pat[p + 2 : end - 1]
                if _cc(name, 0x61) is None:
                    return None  # malformed [:class:]: git aborts
                items.append(("c", name))
                p = end
                prev = 0
            else:  # no ":]": "[" is an ordinary member, the scan goes on at ":"
                items.append(("r", ch, ch))
                prev = ch
        else:
            items.append(("r", ch, ch))
            prev = ch
        p += 1
    # O(members + 256) (#583 round 3, Opus delta M2: the per-byte scan of every member was quadratic): each range is
    # one slice, each POSIX class one precomputed table
    hit = bytearray(256)
    for it in items:
        if it[0] == "r":
            lo, hi = it[1], it[2]  # type: ignore[misc]
            if lo <= hi:
                hit[lo : hi + 1] = b"\x01" * (hi - lo + 1)
        else:
            cls = _CLASSES[it[1]]  # type: ignore[index]
            for c in range(256):
                if cls[c]:
                    hit[c] = 1
    table = bytes(1 if (bool(hit[c]) != negated and c != _SLASH) else 0 for c in range(256))
    return table, p + 1


_CLASSES = {
    name: bytes(1 if _cc(name, c) else 0 for c in range(256))
    for name in (b"alnum", b"alpha", b"blank", b"cntrl", b"digit", b"graph", b"lower", b"print", b"punct", b"space",
                 b"upper", b"xdigit")
}  # fmt: skip


class Budget:
    """Work allowed for one direct-mode tree check: a wall-clock deadline. :meth:`check` reads the clock (per
    pattern, per directory, per ignore file); :meth:`spend` reads it once ``STEP`` units of work have passed (#583
    round 3: per pattern and per directory, not every 4096 units). Past it: :class:`Unavailable` (unknown)."""

    __slots__ = ("deadline", "left")

    STEP = 256

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.left = self.STEP

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise Unavailable("the working tree took too long to check (the reader's time limit)")

    def spend(self, n: int = 1) -> None:
        self.left -= n
        if self.left <= 0:
            self.left = self.STEP
            self.check()


class Glob:
    __slots__ = ("tokens",)

    def __init__(self, tokens: list[tuple[int, object]]) -> None:
        self.tokens = tokens

    def _close(self, states: set[int]) -> set[int]:
        """Add every state reachable without consuming a byte (a star or ``**/`` may match nothing)."""
        toks = self.tokens
        todo = list(states)
        while todo:
            i = todo.pop()
            if i >= 0 and i < len(toks) and toks[i][0] in (_STAR, _DSTAR, _DIRS) and i + 1 not in states:
                states.add(i + 1)
                todo.append(i + 1)
        return states

    def match(self, path: bytes, budget: Budget | None = None) -> bool:
        toks = self.tokens
        n = len(toks)
        states = self._close({0})
        for c in path:
            if not states:
                return False
            if budget is not None:
                budget.spend(len(states))
            nxt: set[int] = set()
            for s in states:
                if s < 0:  # inside a "**/": any byte; a "/" may also end it
                    nxt.add(s)
                    if c == _SLASH:
                        nxt.add(-s)  # -(i + 1) -> i + 1
                    continue
                if s >= n:
                    continue
                kind, arg = toks[s]
                if kind == _LIT:
                    if c == arg:
                        nxt.add(s + 1)
                elif kind == _ONE:
                    if c != _SLASH:
                        nxt.add(s + 1)
                elif kind == _CLS:
                    if arg[c]:  # type: ignore[index]
                        nxt.add(s + 1)
                elif kind == _STAR:
                    if c != _SLASH:
                        nxt.add(s)
                elif kind == _DSTAR:
                    nxt.add(s)
                else:  # _DIRS
                    nxt.add(-(s + 1))
                    if c == _SLASH:
                        nxt.add(s + 1)
            states = self._close(nxt)
        return n in states


def compile_glob(pat: bytes, budget: Budget | None = None) -> Glob | None:
    """The pattern (one ``.gitignore`` line, its ``!``, trailing ``/`` and leading ``/`` already removed) compiled,
    or ``None`` when git's wildmatch would match nothing with it."""
    toks: list[tuple[int, object]] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == 0x2A:  # "*"
            j = i
            while j < n and pat[j] == 0x2A:
                j += 1
            starts = i == 0 or pat[i - 1] == _SLASH
            if j - i >= 2 and starts and j < n and pat[j] == _SLASH:
                toks.append((_DIRS, None))  # "**/": zero or more directories
                i = j + 1
            elif j - i >= 2 and starts and j == n:
                toks.append((_DSTAR, None))  # "/**" at the end, or a lone "**": everything below
                i = j
            else:
                toks.append((_STAR, None))
                i = j
        elif c == 0x3F:  # "?"
            toks.append((_ONE, None))
            i += 1
        elif c == 0x5B:  # "["
            if budget is not None:
                budget.spend(256)  # a class costs its table (#583 round 3, C)
            got = _class(pat, i)
            if got is None:
                return None
            table, i = got
            toks.append((_CLS, table))
        elif c == 0x5C:  # "\"
            if i + 1 >= n:
                return None
            toks.append((_LIT, pat[i + 1]))
            i += 2
        else:
            toks.append((_LIT, c))
            i += 1
    return Glob(toks)
