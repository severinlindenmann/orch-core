"""git's line diff (``xdiff``), ported so that ``diff`` equals ``git diff --no-ext-diff --no-textconv`` byte for byte.

The pieces follow git's ``xdiff/`` (``xprepare.c``, ``xdiffi.c``, ``xemit.c``) and ``xdiff-interface.c`` in their
default configuration for ``git diff``: the Myers algorithm with git's cost heuristics (not ``--minimal``), the
record cleanup that sets aside lines with no or too many matches, the slider compaction with the **indent
heuristic** (``diff.indentHeuristic`` defaults to on), hunks merged when at most ``2 * context`` lines apart
(``diff.interHunkContext`` 0), and the default function-name rule for the hunk header (a line that starts with a
letter, ``_`` or ``$``; no attributes are read). With ``context`` 0 the common tail is trimmed first, as git does.

Lines are compared as raw bytes including their newline, as git compares them with no whitespace options.
"""

from __future__ import annotations

__all__ = ["diff_lines", "split_lines", "unified"]

_MAX_EQLIMIT = 1024
_SIMSCAN_WINDOW = 100
_KPDIS_RUN = 4
_MAX_COST_MIN = 256
_SNAKE_CNT = 20
_HEUR_MIN_COST = 256
_K_HEUR = 4
_LINE_MAX = (1 << 62) - 1

_MAX_INDENT = 200
_MAX_BLANKS = 20
_START_OF_FILE_PENALTY = 1
_END_OF_FILE_PENALTY = 21
_TOTAL_BLANK_WEIGHT = -30
_POST_BLANK_WEIGHT = 6
_RELATIVE_INDENT_PENALTY = -4
_RELATIVE_INDENT_WITH_BLANK_PENALTY = 10
_RELATIVE_OUTDENT_PENALTY = 24
_RELATIVE_OUTDENT_WITH_BLANK_PENALTY = 17
_RELATIVE_DEDENT_PENALTY = 23
_RELATIVE_DEDENT_WITH_BLANK_PENALTY = 17
_INDENT_WEIGHT = 60
_INDENT_HEURISTIC_MAX_SLIDING = 100
_FUNC_LINE_MAX = 80
_SPACE = b" \t\n\r"  # git's own isspace (sane_ctype): no \v, no \f


def split_lines(data: bytes) -> list[bytes]:
    """Records as xdiff makes them: each line with its ``\\n``; a last line without one is a record too."""
    if not data:
        return []
    lines = data.split(b"\n")
    out = [ln + b"\n" for ln in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def _bogosqrt(n: int) -> int:
    i = 1
    while n > 0:
        i <<= 1
        n >>= 2
    return i


class _File:
    """One side: records, their classes, and ``rchg`` with a sentinel on each end (``rchg[i + 1]`` is line ``i``)."""

    def __init__(self, recs: list[bytes], ha: list[int]) -> None:
        self.recs = recs
        self.ha = ha
        self.nrec = len(recs)
        self.rchg = [0] * (self.nrec + 2)
        self.rindex: list[int] = []
        self.ha_r: list[int] = []
        self.dstart = 0
        self.dend = 0


def _clean_mmatch(dis: list[int], i: int, s: int, e: int) -> bool:
    if i - s > _SIMSCAN_WINDOW:
        s = i - _SIMSCAN_WINDOW
    if e - i > _SIMSCAN_WINDOW:
        e = i + _SIMSCAN_WINDOW
    r = 1
    rdis0 = 0
    rpdis0 = 1
    while i - r >= s:
        v = dis[i - r]
        if not v:
            rdis0 += 1
        elif v == 2:
            rpdis0 += 1
        else:
            break
        r += 1
    if rdis0 == 0:
        return False
    r = 1
    rdis1 = 0
    rpdis1 = 1
    while i + r <= e:
        v = dis[i + r]
        if not v:
            rdis1 += 1
        elif v == 2:
            rpdis1 += 1
        else:
            break
        r += 1
    if rdis1 == 0:
        return False
    rdis1 += rdis0
    rpdis1 += rpdis0
    return rpdis1 * _KPDIS_RUN < (rpdis1 + rdis1)


def _prepare(a: list[bytes], b: list[bytes]) -> tuple[_File, _File]:
    classes: dict[bytes, int] = {}
    len1: list[int] = []
    len2: list[int] = []
    ha1 = []
    for ln in a:
        k = classes.get(ln)
        if k is None:
            k = classes[ln] = len(len1)
            len1.append(0)
            len2.append(0)
        len1[k] += 1
        ha1.append(k)
    ha2 = []
    for ln in b:
        k = classes.get(ln)
        if k is None:
            k = classes[ln] = len(len1)
            len1.append(0)
            len2.append(0)
        len2[k] += 1
        ha2.append(k)
    x1, x2 = _File(a, ha1), _File(b, ha2)

    # xdl_trim_ends
    lim = min(x1.nrec, x2.nrec)
    i = 0
    while i < lim and ha1[i] == ha2[i]:
        i += 1
    x1.dstart = x2.dstart = i
    lim -= i
    j = 0
    while j < lim and ha1[x1.nrec - 1 - j] == ha2[x2.nrec - 1 - j]:
        j += 1
    x1.dend = x1.nrec - j - 1
    x2.dend = x2.nrec - j - 1

    # xdl_cleanup_records
    dis1 = [0] * (x1.nrec + 1)
    dis2 = [0] * (x2.nrec + 1)
    mlim = min(_bogosqrt(x1.nrec), _MAX_EQLIMIT)
    for i in range(x1.dstart, x1.dend + 1):
        nm = len2[ha1[i]]
        dis1[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)
    mlim = min(_bogosqrt(x2.nrec), _MAX_EQLIMIT)
    for i in range(x2.dstart, x2.dend + 1):
        nm = len1[ha2[i]]
        dis2[i] = 0 if nm == 0 else (2 if nm >= mlim else 1)
    for x, dis in ((x1, dis1), (x2, dis2)):
        for i in range(x.dstart, x.dend + 1):
            if dis[i] == 1 or (dis[i] == 2 and not _clean_mmatch(dis, i, x.dstart, x.dend)):
                x.rindex.append(i)
                x.ha_r.append(x.ha[i])
            else:
                x.rchg[i + 1] = 1
    return x1, x2


def _split(
    ha1: list[int],
    off1: int,
    lim1: int,
    ha2: list[int],
    off2: int,
    lim2: int,
    kvdf: list[int],
    kvdb: list[int],
    base: int,
    need_min: bool,
    mxcost: int,
) -> tuple[int, int, bool, bool]:
    """``xdl_split``: ``(i1, i2, min_lo, min_hi)``. ``kvdf[d + base]`` is diagonal ``d``."""
    dmin = off1 - lim2
    dmax = lim1 - off2
    fmid = off1 - off2
    bmid = lim1 - lim2
    odd = (fmid - bmid) & 1
    fmin = fmax = fmid
    bmin = bmax = bmid
    kvdf[fmid + base] = off1
    kvdb[bmid + base] = lim1
    ec = 0
    while True:
        ec += 1
        got_snake = False
        if fmin > dmin:
            fmin -= 1
            kvdf[fmin - 1 + base] = -1
        else:
            fmin += 1
        if fmax < dmax:
            fmax += 1
            kvdf[fmax + 1 + base] = -1
        else:
            fmax -= 1
        d = fmax
        while d >= fmin:
            if kvdf[d - 1 + base] >= kvdf[d + 1 + base]:
                i1 = kvdf[d - 1 + base] + 1
            else:
                i1 = kvdf[d + 1 + base]
            prev1 = i1
            i2 = i1 - d
            while i1 < lim1 and i2 < lim2 and ha1[i1] == ha2[i2]:
                i1 += 1
                i2 += 1
            if i1 - prev1 > _SNAKE_CNT:
                got_snake = True
            kvdf[d + base] = i1
            if odd and bmin <= d <= bmax and kvdb[d + base] <= i1:
                return i1, i2, True, True
            d -= 2

        if bmin > dmin:
            bmin -= 1
            kvdb[bmin - 1 + base] = _LINE_MAX
        else:
            bmin += 1
        if bmax < dmax:
            bmax += 1
            kvdb[bmax + 1 + base] = _LINE_MAX
        else:
            bmax -= 1
        d = bmax
        while d >= bmin:
            if kvdb[d - 1 + base] < kvdb[d + 1 + base]:
                i1 = kvdb[d - 1 + base]
            else:
                i1 = kvdb[d + 1 + base] - 1
            prev1 = i1
            i2 = i1 - d
            while i1 > off1 and i2 > off2 and ha1[i1 - 1] == ha2[i2 - 1]:
                i1 -= 1
                i2 -= 1
            if prev1 - i1 > _SNAKE_CNT:
                got_snake = True
            kvdb[d + base] = i1
            if not odd and fmin <= d <= fmax and i1 <= kvdf[d + base]:
                return i1, i2, True, True
            d -= 2

        if need_min:
            continue

        if got_snake and ec > _HEUR_MIN_COST:
            best = 0
            s1 = s2 = 0
            d = fmax
            while d >= fmin:
                dd = d - fmid if d > fmid else fmid - d
                i1 = kvdf[d + base]
                i2 = i1 - d
                v = (i1 - off1) + (i2 - off2) - dd
                if v > _K_HEUR * ec and v > best and off1 + _SNAKE_CNT <= i1 < lim1 and off2 + _SNAKE_CNT <= i2 < lim2:
                    k = 1
                    while ha1[i1 - k] == ha2[i2 - k]:
                        if k == _SNAKE_CNT:
                            best = v
                            s1, s2 = i1, i2
                            break
                        k += 1
                d -= 2
            if best > 0:
                return s1, s2, True, False
            best = 0
            d = bmax
            while d >= bmin:
                dd = d - bmid if d > bmid else bmid - d
                i1 = kvdb[d + base]
                i2 = i1 - d
                v = (lim1 - i1) + (lim2 - i2) - dd
                if v > _K_HEUR * ec and v > best and off1 < i1 <= lim1 - _SNAKE_CNT and off2 < i2 <= lim2 - _SNAKE_CNT:
                    k = 0
                    while ha1[i1 + k] == ha2[i2 + k]:
                        if k == _SNAKE_CNT - 1:
                            best = v
                            s1, s2 = i1, i2
                            break
                        k += 1
                d -= 2
            if best > 0:
                return s1, s2, False, True

        if ec >= mxcost:
            fbest = fbest1 = -1
            d = fmax
            while d >= fmin:
                i1 = min(kvdf[d + base], lim1)
                i2 = i1 - d
                if lim2 < i2:
                    i1 = lim2 + d
                    i2 = lim2
                if fbest < i1 + i2:
                    fbest = i1 + i2
                    fbest1 = i1
                d -= 2
            bbest = bbest1 = _LINE_MAX
            d = bmax
            while d >= bmin:
                i1 = max(off1, kvdb[d + base])
                i2 = i1 - d
                if i2 < off2:
                    i1 = off2 + d
                    i2 = off2
                if i1 + i2 < bbest:
                    bbest = i1 + i2
                    bbest1 = i1
                d -= 2
            if (lim1 + lim2) - bbest < fbest - (off1 + off2):
                return fbest1, fbest - fbest1, True, False
            return bbest1, bbest - bbest1, False, True


def _recs_cmp(x1: _File, x2: _File) -> None:
    ha1, ha2 = x1.ha_r, x2.ha_r
    n1, n2 = len(ha1), len(ha2)
    ndiags = n1 + n2 + 3
    kvdf = [0] * (ndiags + 1)
    kvdb = [0] * (ndiags + 1)
    base = n2 + 1
    mxcost = max(_bogosqrt(ndiags), _MAX_COST_MIN)
    stack = [(0, n1, 0, n2, False)]
    while stack:
        off1, lim1, off2, lim2, need_min = stack.pop()
        while off1 < lim1 and off2 < lim2 and ha1[off1] == ha2[off2]:
            off1 += 1
            off2 += 1
        while off1 < lim1 and off2 < lim2 and ha1[lim1 - 1] == ha2[lim2 - 1]:
            lim1 -= 1
            lim2 -= 1
        if off1 == lim1:
            for k in range(off2, lim2):
                x2.rchg[x2.rindex[k] + 1] = 1
        elif off2 == lim2:
            for k in range(off1, lim1):
                x1.rchg[x1.rindex[k] + 1] = 1
        else:
            i1, i2, min_lo, min_hi = _split(ha1, off1, lim1, ha2, off2, lim2, kvdf, kvdb, base, need_min, mxcost)
            stack.append((i1, lim1, i2, lim2, min_hi))
            stack.append((off1, i1, off2, i2, min_lo))


# -- xdl_change_compact


def _get_indent(rec: bytes) -> int:
    ret = 0
    for c in rec:
        if c not in _SPACE:
            return ret
        if c == 0x20:
            ret += 1
        elif c == 0x09:
            ret += 8 - ret % 8
        if ret >= _MAX_INDENT:
            return _MAX_INDENT
    return -1


def _measure_split(x: _File, split: int) -> tuple[bool, int, int, int, int, int]:
    if split >= x.nrec:
        end_of_file = True
        indent = -1
    else:
        end_of_file = False
        indent = _get_indent(x.recs[split])
    pre_blank = 0
    pre_indent = -1
    i = split - 1
    while i >= 0:
        pre_indent = _get_indent(x.recs[i])
        if pre_indent != -1:
            break
        pre_blank += 1
        if pre_blank == _MAX_BLANKS:
            pre_indent = 0
            break
        i -= 1
    post_blank = 0
    post_indent = -1
    i = split + 1
    while i < x.nrec:
        post_indent = _get_indent(x.recs[i])
        if post_indent != -1:
            break
        post_blank += 1
        if post_blank == _MAX_BLANKS:
            post_indent = 0
            break
        i += 1
    return end_of_file, indent, pre_blank, pre_indent, post_blank, post_indent


def _score_add_split(m: tuple[bool, int, int, int, int, int], score: list[int]) -> None:
    end_of_file, m_indent, pre_blank, pre_indent, m_post_blank, post_indent = m
    if pre_indent == -1 and pre_blank == 0:
        score[1] += _START_OF_FILE_PENALTY
    if end_of_file:
        score[1] += _END_OF_FILE_PENALTY
    post_blank = 1 + m_post_blank if m_indent == -1 else 0
    total_blank = pre_blank + post_blank
    score[1] += _TOTAL_BLANK_WEIGHT * total_blank
    score[1] += _POST_BLANK_WEIGHT * post_blank
    indent = m_indent if m_indent != -1 else post_indent
    any_blanks = total_blank != 0
    score[0] += indent
    if indent == -1 or pre_indent == -1:
        pass
    elif indent > pre_indent:
        score[1] += _RELATIVE_INDENT_WITH_BLANK_PENALTY if any_blanks else _RELATIVE_INDENT_PENALTY
    elif indent == pre_indent:
        pass
    elif post_indent != -1 and post_indent > indent:
        score[1] += _RELATIVE_OUTDENT_WITH_BLANK_PENALTY if any_blanks else _RELATIVE_OUTDENT_PENALTY
    else:
        score[1] += _RELATIVE_DEDENT_WITH_BLANK_PENALTY if any_blanks else _RELATIVE_DEDENT_PENALTY


def _score_cmp(s1: list[int], s2: list[int]) -> int:
    cmp_indents = (s1[0] > s2[0]) - (s1[0] < s2[0])
    return _INDENT_WEIGHT * cmp_indents + (s1[1] - s2[1])


class _Group:
    __slots__ = ("start", "end")

    def __init__(self, x: _File) -> None:
        self.start = 0
        self.end = 0
        while x.rchg[self.end + 1]:
            self.end += 1


def _group_next(x: _File, g: _Group) -> bool:
    """False at the end (git returns -1)."""
    if g.end == x.nrec:
        return False
    g.start = g.end + 1
    g.end = g.start
    while x.rchg[g.end + 1]:
        g.end += 1
    return True


def _group_previous(x: _File, g: _Group) -> bool:
    if g.start == 0:
        return False
    g.end = g.start - 1
    g.start = g.end
    while x.rchg[g.start - 1 + 1]:
        g.start -= 1
    return True


def _group_slide_down(x: _File, g: _Group) -> bool:
    if g.end < x.nrec and x.ha[g.start] == x.ha[g.end]:
        x.rchg[g.start + 1] = 0
        g.start += 1
        x.rchg[g.end + 1] = 1
        g.end += 1
        while x.rchg[g.end + 1]:
            g.end += 1
        return True
    return False


def _group_slide_up(x: _File, g: _Group) -> bool:
    if g.start > 0 and x.ha[g.start - 1] == x.ha[g.end - 1]:
        g.start -= 1
        x.rchg[g.start + 1] = 1
        g.end -= 1
        x.rchg[g.end + 1] = 0
        while x.rchg[g.start - 1 + 1]:
            g.start -= 1
        return True
    return False


def _change_compact(x: _File, xo: _File) -> None:
    g = _Group(x)
    go = _Group(xo)
    while True:
        if g.end != g.start:
            while True:
                groupsize = g.end - g.start
                end_matching_other = -1
                while _group_slide_up(x, g):
                    if not _group_previous(xo, go):
                        raise AssertionError("group sync broken sliding up")
                earliest_end = g.end
                if go.end > go.start:
                    end_matching_other = g.end
                while True:
                    if not _group_slide_down(x, g):
                        break
                    if not _group_next(xo, go):
                        raise AssertionError("group sync broken sliding down")
                    if go.end > go.start:
                        end_matching_other = g.end
                if groupsize == g.end - g.start:
                    break
            if g.end == earliest_end:
                pass
            elif end_matching_other != -1:
                while go.end == go.start:
                    if not _group_slide_up(x, g):
                        raise AssertionError("match disappeared")
                    if not _group_previous(xo, go):
                        raise AssertionError("group sync broken sliding to match")
            else:
                best_shift = -1
                best_score = [0, 0]
                shift = earliest_end
                if g.end - groupsize - 1 > shift:
                    shift = g.end - groupsize - 1
                if g.end - _INDENT_HEURISTIC_MAX_SLIDING > shift:
                    shift = g.end - _INDENT_HEURISTIC_MAX_SLIDING
                while shift <= g.end:
                    score = [0, 0]
                    _score_add_split(_measure_split(x, shift), score)
                    _score_add_split(_measure_split(x, shift - groupsize), score)
                    if best_shift == -1 or _score_cmp(score, best_score) <= 0:
                        best_score = score
                        best_shift = shift
                    shift += 1
                while g.end > best_shift:
                    if not _group_slide_up(x, g):
                        raise AssertionError("best shift unreached")
                    if not _group_previous(xo, go):
                        raise AssertionError("group sync broken sliding to blank line")
        if not _group_next(x, g):
            break
        if not _group_next(xo, go):
            raise AssertionError("group sync broken moving to next group")


def _build_script(x1: _File, x2: _File) -> list[tuple[int, int, int, int]]:
    out = []
    r1, r2 = x1.rchg, x2.rchg
    i1, i2 = x1.nrec, x2.nrec
    while i1 >= 0 or i2 >= 0:
        if r1[i1 - 1 + 1] or r2[i2 - 1 + 1]:
            l1 = i1
            while r1[i1 - 1 + 1]:
                i1 -= 1
            l2 = i2
            while r2[i2 - 1 + 1]:
                i2 -= 1
            out.append((i1, i2, l1 - i1, l2 - i2))
        i1 -= 1
        i2 -= 1
    out.reverse()
    return out


def diff_lines(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, int, int]]:
    """The edit script: ``[(i1, i2, chg1, chg2)]``, ascending."""
    x1, x2 = _prepare(a, b)
    _recs_cmp(x1, x2)
    _change_compact(x1, x2)
    _change_compact(x2, x1)
    return _build_script(x1, x2)


def _trim_common_tail(a: bytes, b: bytes) -> tuple[bytes, bytes]:
    blk = 1024
    trimmed = 0
    smaller = min(len(a), len(b))
    la, lb = len(a), len(b)
    while blk + trimmed <= smaller and a[la - trimmed - blk : la - trimmed] == b[lb - trimmed - blk : lb - trimmed]:
        trimmed += blk
    recovered = 0
    ap = la - trimmed
    while recovered < trimmed:
        c = a[ap + recovered]
        recovered += 1
        if c == 0x0A:
            break
    cut = trimmed - recovered
    return a[: la - cut], b[: lb - cut]


def _func_line(rec: bytes) -> bytes | None:
    if rec and (rec[:1].isalpha() and rec[0] < 0x80 or rec[0] in (0x5F, 0x24)):
        s = rec[:_FUNC_LINE_MAX]
        return s.rstrip(_SPACE)
    return None


def _hunk_header(s1: int, c1: int, s2: int, c2: int, func: bytes) -> bytes:
    out = b"@@ -%d" % (s1 if c1 else s1 - 1)
    if c1 != 1:
        out += b",%d" % c1
    out += b" +%d" % (s2 if c2 else s2 - 1)
    if c2 != 1:
        out += b",%d" % c2
    out += b" @@"
    if func:
        out += b" " + func
    return out + b"\n"


def _record(prefix: bytes, rec: bytes) -> bytes:
    if rec.endswith(b"\n"):
        return prefix + rec
    return prefix + rec + b"\n\\ No newline at end of file\n"


def unified(old: bytes, new: bytes, context: int) -> tuple[list[bytes], int, int]:
    """The hunks of ``git diff`` for two texts: ``(chunks, added, deleted)``; no chunks when they are equal."""
    if context == 0:
        old, new = _trim_common_tail(old, new)
    a, b = split_lines(old), split_lines(new)
    script = diff_lines(a, b)
    out: list[bytes] = []
    added = sum(c[3] for c in script)
    deleted = sum(c[2] for c in script)
    n = len(script)
    k = 0
    funclineprev = -1
    func = b""
    max_common = 2 * context
    while k < n:
        # xdl_get_hunk: the last change of this hunk
        e = k
        while e + 1 < n and script[e + 1][0] - (script[e][0] + script[e][2]) <= max_common:
            e += 1
        i1, i2, _, _ = script[k]
        s1 = max(i1 - context, 0)
        s2 = max(i2 - context, 0)
        ei1, ei2, ec1, ec2 = script[e]
        lctx = min(context, len(a) - (ei1 + ec1), len(b) - (ei2 + ec2))
        e1 = ei1 + ec1 + lctx
        e2 = ei2 + ec2 + lctx
        start, limit = s1 - 1, funclineprev
        step = -1 if start > limit else 1
        line = start
        while line != limit and 0 <= line < len(a):
            f = _func_line(a[line])
            if f is not None:
                func = f
                break
            line += step
        funclineprev = s1 - 1
        out.append(_hunk_header(s1 + 1, e1 - s1, s2 + 1, e2 - s2, func))
        for j in range(s2, i2):
            out.append(_record(b" ", b[j]))
        p1, p2 = i1, i2
        for c in range(k, e + 1):
            ci1, ci2, cc1, cc2 = script[c]
            while p1 < ci1 and p2 < ci2:
                out.append(_record(b" ", b[p2]))
                p1 += 1
                p2 += 1
            for j in range(ci1, ci1 + cc1):
                out.append(_record(b"-", a[j]))
            for j in range(ci2, ci2 + cc2):
                out.append(_record(b"+", b[j]))
            p1, p2 = ci1 + cc1, ci2 + cc2
        for j in range(ei2 + ec2, e2):
            out.append(_record(b" ", b[j]))
        k = e + 1
    return out, added, deleted
