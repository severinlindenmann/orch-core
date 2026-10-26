"""The read routes of #305 (M2): a closed allowlist, one typed route per host op, adapters between.

Nothing here forwards an operation name or free-form arguments from the browser: every route has a pydantic input
model that forbids extras, and the host op and its arguments are fixed by the route.
"""

import base64
import hashlib
import re
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import adapters
from .errors import HostError, fail
from .gateway import WORKSPACE_ID, Gateway

Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$", max_length=26)]
Person = Annotated[str, StringConstraints(pattern=r"^p_[A-Za-z0-9]{1,64}$")]
Word = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")]
ArtifactName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
UiStatus = Literal["backlog", "open", "in-progress", "testing", "done", "closed"]
MAX_ROWS = 1000  # board and Today read at most this many rows; a larger workspace shows the newest
MAX_PAGES = 30  # a page may hold fewer rows than asked (rows drop out between pages): follow next_page, but never loop
RECENT = 6  # Today's "Recently" shows this many activity rows
RECENT_PAGES = 5
CHUNK = 786432
MAX_ARTIFACT = 7 * CHUNK  # 5.25 MiB: a pasted screenshot (at most 5 MiB) is read whole


class TicketsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: list[UiStatus] | None = Field(default=None, min_length=1, max_length=6)
    type: list[Word] | None = Field(default=None, min_length=1, max_length=16)
    label: list[Word] | None = Field(default=None, min_length=1, max_length=16)
    assignee: Person | None = None
    owner: Person | None = None
    parent: Key | None = None
    epic: Key | None = None
    q: str | None = Field(default=None, min_length=1, max_length=200)
    needs_me: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)
    page: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_.=-]+$")
    sort: Literal["updated", "priority", "key"] = "updated"
    order: Literal["asc", "desc"] = "desc"


class EventsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: int = Field(default=0, ge=0, le=2**31)
    limit: int = Field(default=100, ge=1, le=500)


class ChangesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: Word | None = None
    context: int = Field(default=3, ge=0, le=10)


class ContentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


def workspace(ws: str):
    if not WORKSPACE_ID.fullmatch(ws):
        raise fail("invalid.input", exit=5)
    return ws


def host_args(query: TicketsQuery) -> dict:
    args = query.model_dump(exclude_none=True)
    args["status"] = [adapters.host_status(s) for s in args.get("status") or adapters.board_statuses()]
    return args


def all_rows(gateway: Gateway, ws: str, args: dict, cap: int = MAX_ROWS):
    """Every row of one snapshot (the first page fixes it), at most `cap`. Pages may be short or empty: only
    `next_page` says whether more follow, never a count or a page size."""
    rows, page, last = [], None, {}
    for _ in range(MAX_PAGES):
        data = gateway.call(ws, "ticket.query", {**args, "limit": 200, **({"page": page} if page else {})})
        rows += data["rows"]
        last = data
        page = data["next_page"]
        if not page or len(rows) >= cap:
            break
    return rows[:cap], last


def waiting_runs(gateway: Gateway, ws: str) -> list[dict]:
    """The person's live agent runs the host marks `waiting` ("Agent may need you", run-9 feedback 3.6): `{run, ticket,
    waiting}` only. A host without runs, or one that cannot say, gives none."""
    link = gateway.link(ws)
    if not link.supports("run.list", "run/1"):
        return []
    try:
        data = gateway.call(ws, "run.list", {"state": "live", "kind": "agent"})
    except HostError:
        return []
    return [
        {"run": r["run"], "ticket": r.get("ticket"), "waiting": r["waiting"]}
        for r in data.get("runs") or []
        if isinstance(r, dict) and isinstance(r.get("waiting"), dict) and isinstance(r.get("run"), str)
    ]


def recent_activity(gateway: Gateway, ws: str, want: int = RECENT):
    """Today's feed: ticket events only (`scope: tickets`; workspace-log events name members, devices and settings and
    belong on no ticket row). A page may be short under the host's scan budget, so follow `next_page`."""
    link = gateway.link(ws)
    if not link.supports("activity.list", "activity/1"):
        return []
    items, page, seen = [], None, set()
    for _ in range(RECENT_PAGES):
        data = gateway.call(ws, "activity.list", {"scope": "tickets", "limit": 50, **({"page": page} if page else {})})
        for a in data["items"]:
            if a["id"] not in seen:  # the live tail dedupes by id; so does this read
                seen.add(a["id"])
                items.append(a)
        page = data["next_page"]
        if not page or len(items) >= want:
            break
    return items[:want]


PASTE_NAME = re.compile(r"^paste-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.(png|webp)$")


def pasted_by_person(gateway: Gateway, ws: str, key: str, name: str, digest: str) -> bool:
    """R3 part B (lead decision): a picture is drawn inline only when a PERSON signed its `artifact.added` as a
    screenshot with a paste name. An agent's image stays a placeholder with a link to the artifact."""
    if not PASTE_NAME.fullmatch(name):
        return False
    data = gateway.call(ws, "ticket.get", {"ref": key})
    art = next((a for a in data.get("artifacts") or [] if a.get("name") == name), None)
    return bool(
        art
        and art.get("kind") == "screenshot"
        and (art.get("by") or {}).get("kind") == "person"
        and art.get("digest") == digest  # the bytes read are the ones the ticket names for THIS signed artifact
    )


def picture(data: bytes) -> dict | None:
    from orch.host import imagecheck

    try:
        kind = imagecheck.check(data)
    except imagecheck.ImageRefused:
        return None
    return {
        "type": "png" if kind == "image/png" else "webp",
        "b64": base64.b64encode(data).decode(),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


SVG_MAX = 512 * 1024
GIF_FRAMES_MAX = 500
SVG_ELEMENTS_MAX = 5000
SVG_DEPTH_MAX = 32
_BOM = b"\xef\xbb\xbf"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
#: the only elements an evidence SVG may use. No script, style, use, image, foreignObject, filter, a or animation.
_SVG_ELEMENTS = frozenset(
    "svg g path rect circle ellipse line polyline polygon text tspan defs "
    "linearGradient radialGradient stop title desc clipPath".split()
)
#: the only attributes an evidence SVG may carry (no `style`, no events, no presentation attribute that can name a URL
#: other than fill/stroke/clip-path with `url(#id)`). Default deny: anything else refuses the whole file.
_SVG_ATTRS = frozenset(
    "id x y width height cx cy r rx ry x1 y1 x2 y2 d points viewBox preserveAspectRatio transform fill stroke "
    "stroke-width stroke-linecap stroke-linejoin stroke-dasharray opacity fill-opacity stroke-opacity fill-rule "
    "clip-rule clip-path offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod fx fy font-size "
    "font-family font-weight text-anchor dominant-baseline dx dy version href".split()
)
_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)", re.IGNORECASE)
_REF = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos);)")
_FUNC = re.compile(r"([A-Za-z0-9_-]*)\s*\(")
#: the only functions an attribute value may call besides `url(#id)`: colours and transforms
_SAFE_FUNCS = frozenset("url rgb rgba hsl hsla matrix translate scale rotate skewx skewy".split())
_URL_FN = re.compile(r"url\(", re.IGNORECASE)
_URL_LOCAL = re.compile(r"url\(\s*#[\w.\-]+\s*\)", re.IGNORECASE)


def is_svg(data: bytes) -> bool:
    """The ONE rule for "these bytes are an SVG" (the page's `sniffImage` in pasteImage.ts uses the same one):
    the first 512 bytes, without a UTF-8 BOM, trimmed and lower-cased, start with `<svg`, or with `<?xml` /
    `<!doctype svg` and an `<svg` tag shows up in the first 4096 bytes."""
    data = data.removeprefix(_BOM)
    lead = data[:512].lstrip().lower()
    return lead.startswith(b"<svg") or (
        lead.startswith((b"<?xml", b"<!doctype svg")) and b"<svg" in data[:4096].lower()
    )


def safe_svg(data: bytes) -> bool:
    """Deny by default: only listed elements and attributes pass; values are never trusted to be safe.

    An ALLOWLIST, not a scan: the document parses as strict XML with no DTD, entity or processing instruction, every
    element is in the SVG namespace and in `_SVG_ELEMENTS`, no attribute can load or run anything (`on*`, a reference
    that is not `#fragment`, `data:`, `javascript:`, `url()` other than `url(#id)`), and element count and depth are
    bounded. Anything else, including a parse error, is refused."""
    from xml.parsers import expat

    if len(data) > SVG_MAX or _REF.search(data):  # numeric and named references other than the five predefined
        return False
    declared = _XML_ENCODING.match(data.removeprefix(_BOM))
    if declared and declared.group(1).lower() not in (b"utf-8", b"utf8"):
        return False
    state = {"n": 0, "depth": 0, "root": False}

    class Refused(Exception):
        pass

    def no(*_a):
        raise Refused

    def start(name, attrs):
        state["n"] += 1
        state["depth"] += 1
        if state["n"] > SVG_ELEMENTS_MAX or state["depth"] > SVG_DEPTH_MAX:
            raise Refused
        ns, _, local = name.rpartition(" ")
        if ns != _SVG_NS or local not in _SVG_ELEMENTS or (not state["root"] and local != "svg"):
            raise Refused
        state["root"] = True
        for key, value in attrs.items():
            kns, _, klocal = key.rpartition(" ")
            if (
                (kns == "" and klocal not in _SVG_ATTRS)
                or (kns == _XLINK_NS and klocal != "href")
                or kns not in ("", _XLINK_NS)
            ):
                raise Refused
            low = value.lower()
            if "\\" in value or "data:" in low or "javascript:" in low or "&" in value:
                raise Refused
            if klocal == "href" and not value.startswith("#"):
                raise Refused
            if len(_URL_FN.findall(value)) != len(_URL_LOCAL.findall(value)):
                raise Refused
            if any(f.lower() not in _SAFE_FUNCS for f in _FUNC.findall(value)):  # any other "(": refuse
                raise Refused

    def end(_name):
        state["depth"] -= 1

    def ns_decl(_prefix, uri):
        if uri not in (_SVG_NS, _XLINK_NS):
            raise Refused

    parser = expat.ParserCreate("UTF-8", namespace_separator=" ")
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartNamespaceDeclHandler = ns_decl
    parser.StartDoctypeDeclHandler = no
    parser.EntityDeclHandler = no
    parser.ProcessingInstructionHandler = no
    parser.ExternalEntityRefHandler = no
    try:
        parser.Parse(data.removeprefix(_BOM), True)
    except (Refused, expat.ExpatError):
        return False
    return state["root"]


def _gif_ok(data: bytes, side_max: int, pixels_max: int) -> bool:
    """Walk the GIF's blocks, headers only: the screen and EVERY frame must lie inside the size limits and inside the
    screen, the frame count is bounded, the walk ends exactly on the 0x3B trailer. A browser enlarges the canvas to a
    frame that sticks out, so the logical screen alone proves nothing."""
    n = len(data)
    if n < 13:
        return False
    sw, sh, packed = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"), data[10]
    if not (1 <= sw <= side_max and 1 <= sh <= side_max) or sw * sh > pixels_max:
        return False
    i = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
    frames, area = 0, 0

    def skip_blocks(pos: int) -> int:
        while True:
            if pos >= n:
                return -1
            size = data[pos]
            pos += 1 + size
            if size == 0:
                return pos

    while i < n:
        block = data[i]
        if block == 0x3B:
            return frames >= 1
        if block == 0x21:
            i = skip_blocks(i + 2)
        elif block == 0x2C:
            if i + 10 > n:
                return False
            left, top, w, h = (int.from_bytes(data[i + 1 + k : i + 3 + k], "little") for k in (0, 2, 4, 6))
            local = data[i + 9]
            if not (1 <= w <= side_max and 1 <= h <= side_max) or left + w > sw or top + h > sh:
                return False
            frames += 1
            area += w * h
            if frames > GIF_FRAMES_MAX or area > 5 * pixels_max or frames * sw * sh > 5 * pixels_max:
                return False
            i += 10 + (3 * 2 ** ((local & 7) + 1) if local & 0x80 else 0) + 1  # + LZW minimum code size
            if i > n:
                return False
            i = skip_blocks(i)
        else:
            return False
        if i < 0:
            return False
    return False  # no trailer


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the first SOFn segment, read from headers only (no decoding)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
        if length < 2:
            return None
        i += 2 + length
    return None


def media(data: bytes) -> dict | None:
    """The bytes of an evidence picture (PNG, JPEG, GIF, WebP, SVG), typed by its first bytes, never by the name.

    Run only on a complete file whose sha256 matched the ticket's record. It is a read-only offer: no decoding here, and
    the page hashes the bytes again and shows them only as an <img> from a data: URL (an image context runs no script,
    loads nothing, and SVG is never drawn as markup). Size limits are core's: a picture side is at most SIDE_MAX pixels
    and the area PIXELS_MAX (a decompression-bomb header is refused without decoding anything); PNG and WebP also go
    through core's own header check. An SVG is at most 512 KiB and has no entity declaration or internal DTD subset.
    """
    from orch.host import imagecheck

    if data.startswith(b"\x89PNG\r\n\x1a\n") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        try:
            kind = "png" if imagecheck.check(data) == "image/png" else "webp"
        except imagecheck.ImageRefused:
            return None
    elif data.startswith(b"\xff\xd8\xff"):
        kind, size = "jpeg", _jpeg_size(data)
        if not size or not (1 <= size[0] <= imagecheck.SIDE_MAX and 1 <= size[1] <= imagecheck.SIDE_MAX):
            return None
        if size[0] * size[1] > imagecheck.PIXELS_MAX:
            return None
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        if not _gif_ok(data, imagecheck.SIDE_MAX, imagecheck.PIXELS_MAX):
            return None
        kind = "gif"
    elif is_svg(data):
        if not safe_svg(data):
            return None
        kind = "svg"
    else:
        return None
    return {
        "type": kind,
        "b64": base64.b64encode(data).decode(),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def register(app: FastAPI, gateway: Gateway):
    def viewer(ws):
        return gateway.link(ws).welcome["peer"]["person"]

    def needs(ws):
        return gateway.call(ws, "needs")

    @app.get("/api/w/{ws}/today")
    def today(ws: str = Depends(workspace)):
        who = viewer(ws)
        data = needs(ws)
        rows, last = all_rows(gateway, ws, {"status": adapters.board_statuses(), "sort": "updated"})
        role = next((m.role for m in adapters.members(gateway.call(ws, "member.list")) if m.person == who), "viewer")
        # An open question on a closed ticket is still a need: read the closed rows only then, for their titles.
        closed = []
        if any(n.get("status") == "closed" for n in data["items"]):
            closed, _ = all_rows(gateway, ws, {"status": ["closed"], "sort": "updated"})
        from .hubapprove import HUB_ONLY, mode_of

        sign = "hub" if mode_of(gateway, ws) in HUB_ONLY else None  # V3 C11: full hub mode, approved elsewhere
        return adapters.today(
            data, rows, ws, last.get("as_of") or "", who, role, recent_activity(gateway, ws), closed, sign,
            waiting_runs(gateway, ws),
        )

    def listing(ws: str, query: TicketsQuery):
        who = viewer(ws)
        data = gateway.call(ws, "ticket.query", host_args(query))
        return adapters.page(data, needs(ws), who)

    @app.get("/api/w/{ws}/tickets")
    def tickets(query: Annotated[TicketsQuery, Query()], ws: str = Depends(workspace)):
        return listing(ws, query)

    @app.get("/api/w/{ws}/board")
    def board(query: Annotated[TicketsQuery, Query()], ws: str = Depends(workspace)):
        return listing(ws, query)

    def mine(ws):
        out = {}
        for n in needs(ws)["items"]:
            if n.get("uid"):
                out.setdefault(n["uid"], []).append(n)
        return out

    @app.get("/api/w/{ws}/tickets/{key}")
    def ticket(key: Key, ws: str = Depends(workspace)):
        # A plain read of a visible closed or done ticket never errors (v1.4 section 0.8.4): the page draws its state.
        data = gateway.call(ws, "ticket.get", {"ref": key})
        return adapters.document(data, viewer(ws), mine(ws), ext=gateway.link(ws).has("read_ext/1"))

    @app.get("/api/w/{ws}/tickets/{key}/events")
    def ticket_events(key: Key, query: Annotated[EventsQuery, Query()], ws: str = Depends(workspace)):
        data = gateway.call(ws, "ticket.get", {"ref": key, "log_since": query.since, "log_limit": query.limit})
        from .adapters.activity import acceptance_authority

        roster = gateway.call(ws, "member.list")
        return adapters.events(data, acceptance_authority(roster, data["doc"]))

    @app.get("/api/w/{ws}/tickets/{key}/changes")
    def ticket_changes(key: Key, query: Annotated[ChangesQuery, Query()], ws: str = Depends(workspace)):
        args = {"ref": key, "context": query.context, **({"repo": query.repo} if query.repo else {})}
        return adapters.changes(gateway.call(ws, "diff", args), key)

    @app.get("/api/w/{ws}/tickets/{key}/artifacts/{name}")
    def artifact(key: Key, name: ArtifactName, query: Annotated[ContentQuery, Query()], ws: str = Depends(workspace)):
        """An evidence file, read in chunks up to a cap, its digest checked against the one the ticket names."""
        chunks, offset = [], 0
        for _ in range(MAX_ARTIFACT // CHUNK + 1):
            part = gateway.call(ws, "artifact.get", {"ref": key, "name": name, "offset": offset, "length": CHUNK})
            raw = base64.b64decode(part["b64"], validate=True)
            if not raw and not part["last"]:
                raise fail("internal")  # a host that makes no progress must not pin a worker thread
            chunks.append(raw)
            offset += len(raw)
            if part["last"] or offset >= MAX_ARTIFACT:
                break
        else:
            raise fail("internal")
        data = b"".join(chunks)
        complete = bool(part["last"])
        digest = part["digest"]
        verified = complete and hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
        if complete and not verified:
            raise fail("artifact.mismatch", "The file does not match its recorded digest.", exit=3)
        try:
            # A cut-off file may end inside a character: drop that tail instead of calling the file binary.
            text, binary = data.decode("utf-8", errors="ignore" if not complete else "strict"), False
        except UnicodeDecodeError:
            text, binary = None, True
        # A person's pasted screenshot keeps its own `image` offer (ticket text draws it); any other verified picture is
        # offered as `media`. The bytes travel once, and a picture's `text` copy is never sent.
        pasted = picture(data) if verified and pasted_by_person(gateway, ws, key, name, digest) else None
        offered = None if pasted else media(data) if verified else None
        return {
            "name": name,
            "size": part["size"],
            "digest": digest,
            "verified": verified,
            "truncated": not complete,
            "binary": binary,
            "text": None if (offered or pasted) else text,
            # R3 part B: a screenshot pasted into the ticket. Only a complete, verified PNG/WebP that passes core's
            # header check is offered; the page hashes the bytes itself before showing them, and only ever as an <img>.
            # Evidence preview (#1069): any complete, digest-verified picture, whoever added it. The page re-hashes it.
            "image": pasted,
            "media": offered,
        }
