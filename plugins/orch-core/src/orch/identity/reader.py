"""The reader-version handshake (orch-v2-host §0.8.6 item 9, H12d): a reader's signed declaration and its check.

A reader (a phone, a member's core) declares the highest level it understands, with the known-answer test (KAT)
digests of every parsing surface it uses, signed by its device key::

    reader_decl = {"o": {"v": 1, "kind": "reader_decl", "suite": 2, "workspace_id", "device_id", "dk_hash",
                         "who": {"person_id"} | {"pk_pin"}, "reader", "kat": {"1": {"log"?, "view"?}, ...},
                         "bind": {"via": "pair", "offer_id", "attempt"} | {"via": "enrol"} | {"via": "declare"},
                         "created_ms"},
                   "sig": b64u(Sign(dk_sig, "orch/v2/sig/reader-decl|" || cj(o)))}

:func:`verify_decl` is the appending writer's check (9.3), pure and stateless. Its refusals are
``reader.decl_invalid`` with a ``reason``, in this order: ``shape`` (the exact field set, the 2 KiB cap of the whole
signed object, every value's form), ``signature`` (under the request's ``dk_sig``), ``workspace``, ``device``,
``keys`` (``dk_hash`` of the request's two keys), ``who``, ``bind``, ``time`` (core's per-``via`` window), then per
level ``kat_missing`` and ``kat_mismatch``. The reason names are the relay reference's (orch-relay
``ref/orch_protocol_ref.py`` ``verify_reader_decl``), which core's tests consume once the corpus is vendored.

Decisions (H12d-1, the strictest reading where 9.3 leaves room):

* Ids are the protocol's bare lower-case hex (``workspace_id``, ``device_id``, ``person_id``: 32 hex), never F1's
  ``p_``/``d_`` forms; ``dk_hash`` and ``pk_pin`` are canonical ``b64u`` of 32 bytes.
* A ``kat`` level key is ``[1-9][0-9]*`` and at most ``reader``: a declaration does not carry digests for a level it
  does not declare. Entries of levels above ``host_max`` are otherwise ignored (an unknown higher level is accepted as
  ``host_max``, 9.3); entries up to ``host_max`` name only known surfaces, each a ``sha256:`` + 64 lower-case hex.
* ``created_ms`` more than 300 s ahead of the host clock is refused for every ``via`` (9.3 "Latest wins"); ``enrol``
  also refuses one more than 30 days old; ``declare`` must be within ±300 s. ``now_ms=None, recheck=True`` is a
  recheck (a binding, a submit), which never reads the clock (9.3 "Freshness is checked once"); either alone is a
  caller bug (#463 review L2).
* The expected digests are core's own (:data:`KAT_DIGESTS`, pinned by ``tests/vectors/f1/reader_kat_1.json``); a
  caller passes others only to consume foreign vectors.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

from orch import canon, crypto

from .errors import Refused

__all__ = [
    "ENROL_MAX_AGE_MS",
    "FUTURE_MS",
    "HOST_MAX",
    "KAT_DIGESTS",
    "LABEL",
    "LEVEL_MAX",
    "MAX_BYTES",
    "READER_VIEWS_1",
    "SURFACES",
    "DeclInvalid",
    "Declared",
    "auth_values",
    "decl_digest",
    "kat_digest",
    "parse_signer",
    "run_kat",
    "verify_decl",
]

LABEL = canon.LABELS["sig_reader_decl"].encode("ascii")
MAX_BYTES = 2048  # of cj(reader_decl), the whole signed object (9.12)
LEVEL_MAX = 2**31 - 1
HOST_MAX = 1  # the highest reader level this core knows (level 1: understands `auth: "convenience"`, refuses unknown)
SURFACES = ("log", "view")
FUTURE_MS = 300_000
ENROL_MAX_AGE_MS = 30 * 86_400_000
CODE = "reader.decl_invalid"
READER_VIEWS_1 = ("ticket.get.questions.answer",)
"""The view entries whose ``signer: {device, auth}`` a level-1 reader parses (9.11), in corpus order. Owner decision
with orch-f1 (#463 round 1): only the answer of a question in ``ticket.get`` (``questions[].answer.signer``), the one
place the phone shows a signer. A later phone build that renders a signer elsewhere needs ``READER_VIEWS_2`` (a new
corpus and a new level), never a silent change of this list."""
KAT_DIGESTS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "1": MappingProxyType(
            {
                "log": "sha256:70b450df574b6c372005b7504a7f9efe80552cb2841193b960fe538c6aa6fc0e",
                "view": "sha256:d76e4593ec191e91ca2f6bddef570cbfa1af14dae702bb5ded0572eebe1d9085",
            }
        )
    }
)
"""The expected ``reader_kat_1`` digests per level and surface (``tests/vectors/f1/reader_kat_1.json`` ``expected``;
``tests/identity/test_reader_kat.py`` checks that core's own runner reproduces them)."""

_FIELDS = frozenset({"v", "kind", "suite", "workspace_id", "device_id", "dk_hash", "who", "reader", "kat", "bind",
                     "created_ms"})  # fmt: skip
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LEVEL_KEY = re.compile(r"[1-9][0-9]*")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_MS_MAX = 2**53 - 1


class DeclInvalid(Refused):
    """``reader.decl_invalid`` (exit 3, not retryable) with the ``reason`` of the first failed check."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(CODE, f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class Declared(NamedTuple):
    """A verified declaration: the declaring device, the declared ``reader``, the ``level`` it counts as here
    (``min(reader, host_max)``) and ``digest`` (``rd``, :func:`decl_digest`)."""

    device_id: str
    reader: int
    level: int
    digest: str


def decl_digest(decl: Mapping[str, Any]) -> str:
    """``rd`` (protocol §8.6) and a reader record's ``digest``: ``"sha256:" + hex(sha256(cj(reader_decl)))``."""
    return "sha256:" + hashlib.sha256(canon.cj_checked(decl)).hexdigest()


def _uint(v: object, top: int) -> bool:
    return type(v) is int and 0 <= v <= top


def _hex32(v: object) -> bool:
    return type(v) is str and _HEX32.fullmatch(v) is not None


def _b64u32(v: object) -> bool:
    try:
        crypto.unb64u(v, 32)
    except crypto.EncodingError:
        return False
    return True


def _bind_ok(bind: object) -> bool:
    if type(bind) is not dict or bind.get("via") not in ("pair", "enrol", "declare"):
        return False
    if bind["via"] == "pair":
        return set(bind) == {"via", "offer_id", "attempt"} and _hex32(bind["offer_id"]) and _hex32(bind["attempt"])
    return set(bind) == {"via"}


def _kat_ok(kat: object, reader: int, host_max: int) -> bool:
    if type(kat) is not dict:
        return False
    for k, entry in kat.items():
        if type(k) is not str or not _LEVEL_KEY.fullmatch(k) or type(entry) is not dict:
            return False
        level = int(k)
        if level > reader:  # digests for a level the declaration does not claim (H12d-1, strict)
            return False
        if level > host_max:  # a higher level's surfaces are unknown here: ignored (9.3)
            continue
        if not entry or not set(entry) <= set(SURFACES):
            return False
        if not all(type(d) is str and _DIGEST.fullmatch(d) for d in entry.values()):
            return False
    return True


def _shape(decl: object, host_max: int) -> tuple[dict[str, Any], bytes]:
    if type(decl) is not dict or set(decl) != {"o", "sig"} or type(decl["o"]) is not dict:
        raise DeclInvalid("shape", "not a signed object {o, sig}")
    try:
        size = len(canon.cj_checked(decl))
    except (canon.HashError, RecursionError):
        raise DeclInvalid("shape", "not canonical JSON") from None
    if size > MAX_BYTES:
        raise DeclInvalid("shape", f"{size} bytes, the cap is {MAX_BYTES}")
    o = decl["o"]
    ok = (
        set(o) == _FIELDS
        and type(o["v"]) is int
        and o["v"] == 1
        and o["kind"] == "reader_decl"
        and type(o["suite"]) is int
        and o["suite"] == crypto.SUITE_ID
        and _hex32(o["workspace_id"])
        and _hex32(o["device_id"])
        and _b64u32(o["dk_hash"])
        and type(o["who"]) is dict
        and len(o["who"]) == 1
        and (_hex32(o["who"].get("person_id")) or _b64u32(o["who"].get("pk_pin")))
        and _uint(o["reader"], LEVEL_MAX)
        and _uint(o["created_ms"], _MS_MAX)
        and _bind_ok(o["bind"])
        and _kat_ok(o["kat"], o["reader"], host_max)
    )
    if not ok:
        raise DeclInvalid("shape", "a field is missing, unknown or malformed")
    try:
        sig = crypto.unb64u(decl["sig"], crypto.SIG_LEN)
    except crypto.EncodingError:
        raise DeclInvalid("shape", "sig is not 64 bytes of canonical base64url") from None
    return o, sig


def _time(o: Mapping[str, Any], now_ms: int | None) -> None:
    if now_ms is None:
        return
    t, via = o["created_ms"], o["bind"]["via"]
    late = t > now_ms + FUTURE_MS
    early = (via == "enrol" and t < now_ms - ENROL_MAX_AGE_MS) or (via == "declare" and t < now_ms - FUTURE_MS)
    if late or early:
        raise DeclInvalid("time", f"created_ms {t} is outside the {via} window; the host's time is {now_ms}")


def _context(workspace_id, dk_sig_pub, dk_kx_pub, who, bind, surfaces, now_ms, recheck, host_max, expected):
    if not _hex32(workspace_id):
        raise ValueError("workspace_id must be 32 lower-case hex")
    crypto.validate_public_key(dk_sig_pub)
    crypto.validate_public_key(dk_kx_pub)
    if not isinstance(who, Mapping) or not _bind_ok(dict(bind) if isinstance(bind, Mapping) else bind):
        raise ValueError("who and bind must be the request's binding")
    req = tuple(surfaces)
    if not req or len(set(req)) != len(req) or not set(req) <= set(SURFACES):
        raise ValueError(f"surfaces must be a non-empty subset of {SURFACES}")
    if type(recheck) is not bool:
        raise ValueError("recheck must be a bool")
    if recheck and now_ms is not None:
        raise ValueError("a recheck reads no clock: pass now_ms=None with recheck=True")
    if not recheck and not _uint(now_ms, _MS_MAX):
        raise ValueError("now_ms must be the host clock in ms; only an explicit recheck=True passes now_ms=None")
    if not _uint(host_max, LEVEL_MAX) or not isinstance(expected, Mapping):
        raise ValueError("host_max and expected must be given")
    for k in range(1, host_max + 1):
        entry = expected.get(str(k))
        if not isinstance(entry, Mapping) or not all(s in entry for s in req):
            raise ValueError(f"no expected KAT digest for level {k} and surfaces {req}")
    return req


def verify_decl(
    decl: object,
    *,
    workspace_id: str,
    dk_sig_pub: bytes,
    dk_kx_pub: bytes,
    who: Mapping[str, str],
    bind: Mapping[str, str],
    surfaces: Iterable[str],
    now_ms: int | None,
    recheck: bool = False,
    expected: Mapping[str, Mapping[str, str]] = KAT_DIGESTS,
    host_max: int = HOST_MAX,
) -> Declared:
    """Check ``decl`` against the request that carries it (9.3) and return what it declares, or raise
    :class:`DeclInvalid`.

    ``workspace_id`` is the request's workspace (a pairing: the offer stream's); ``dk_sig_pub``/``dk_kx_pub`` the
    request's device keys (``member.add``: the device certificate's; a pairing: the pair meta's; ``reader.declare``:
    the requesting device's certificate); ``who`` the person binding the host expects (``{"person_id": hex}``, or
    ``{"pk_pin": b64u}`` for a pairing without a certificate); ``bind`` the expected ``bind`` exactly; ``surfaces``
    the KAT surfaces the host requires (``log`` from a core, ``view`` from a phone); ``now_ms`` the host clock at
    admission; a recheck (a binding, a submit: never the clock) passes ``now_ms=None`` **and** ``recheck=True``, and
    either alone is a :class:`ValueError`, so freshness is never dropped by accident; ``expected`` level -> surface ->
    digest, every level up to ``host_max``.
    A bad context (not the declaration) raises :class:`ValueError`: it is the caller's bug, never the reader's."""
    req = _context(workspace_id, dk_sig_pub, dk_kx_pub, who, bind, surfaces, now_ms, recheck, host_max, expected)
    o, sig = _shape(decl, host_max)
    if not crypto.verify(dk_sig_pub, sig, LABEL + canon.cj_checked(o)):
        raise DeclInvalid("signature", "not signed by the request's device key under orch/v2/sig/reader-decl|")
    if o["workspace_id"] != workspace_id:
        raise DeclInvalid("workspace", "the declaration names another workspace")
    if o["device_id"] != crypto.device_id(dk_sig_pub).hex():
        raise DeclInvalid("device", "device_id is not derived from the request's dk_sig_pub")
    if o["dk_hash"] != crypto.b64u(crypto.dk_hash(dk_sig_pub, dk_kx_pub)):
        raise DeclInvalid("keys", "dk_hash is not the request's two device keys")
    if o["who"] != dict(who):
        raise DeclInvalid("who", "the declaration names another person")
    if o["bind"] != dict(bind):
        raise DeclInvalid("bind", "the declaration is bound to another attempt or path")
    _time(o, now_ms)
    level = min(o["reader"], host_max)
    for k in range(1, level + 1):
        entry = o["kat"].get(str(k))
        if entry is None or not all(s in entry for s in req):
            raise DeclInvalid("kat_missing", f"level {k} needs the KAT digest of {', '.join(req)}")
        want = expected[str(k)]
        if any(entry[s] != want[s] for s in req):
            raise DeclInvalid("kat_mismatch", f"level {k}: a KAT digest is not the expected one")
    return Declared(o["device_id"], o["reader"], level, decl_digest(decl))


# --- the known-answer test (9.2) -------------------------------------------------------------------------------------


def kat_digest(results: list[dict[str, Any]]) -> str:
    """F1 §11.5: ``"sha256:" + hex(sha256(cj(results)))``, the results in corpus order."""
    return "sha256:" + hashlib.sha256(canon.cj_checked(results)).hexdigest()


def auth_values() -> tuple[str, ...]:
    """The ``auth`` values a reader of level 1 knows: exactly the event schema's enum (F1 §11.4), one source."""
    from orch.schema import load

    return tuple(load("common")["$defs"]["auth"]["enum"])


_DEVICE_REF = re.compile(r"d_[0-9a-f]{32}")


def _signer_of(view: str, entry: object) -> object:
    if view == "ticket.get.questions.answer":
        if type(entry) is not dict or type(entry.get("answer")) is not dict or "signer" not in entry["answer"]:
            raise ValueError("a ticket.get question entry carries answer.signer")
        return entry["answer"]["signer"]
    raise ValueError(f"{view!r} is not one of {READER_VIEWS_1}")


def parse_signer(view: str, entry: object) -> str:
    """The production parser of the ``signer`` of a ``READER_VIEWS_1`` entry (9.11): for
    ``ticket.get.questions.answer`` the ``answer.signer`` of a Question; exactly ``{device, auth}``, a ``d_`` device
    id and a known ``auth``; returns the ``auth``. Anything else raises :class:`ValueError`: an unknown ``auth`` is
    refused, never mapped to a known one (level 1). H12d-2 checks every emitted ``signer`` with it."""
    signer = _signer_of(view, entry)
    if type(signer) is not dict or set(signer) != {"device", "auth"}:
        raise ValueError("signer is exactly {device, auth}")
    if type(signer["device"]) is not str or not _DEVICE_REF.fullmatch(signer["device"]):
        raise ValueError("signer.device is a d_ device id")
    if type(signer["auth"]) is not str or signer["auth"] not in auth_values():
        raise ValueError(f"unknown signer.auth {signer['auth']!r}")
    return signer["auth"]


def _log_events(context: Mapping[str, Any], lines: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Read ``lines`` as a workspace log with the production line parser; ``None`` if a line is refused."""
    from orch.store.logs import LINE_ERRORS, MAX_LINE, parse_line

    out = []
    for seq, e in enumerate(lines, 1):
        try:
            line = canon.cj_checked(e) + b"\n"
            if len(line) > MAX_LINE:
                return None
            ev, _ = parse_line(line, seq, "workspace")
        except LINE_ERRORS:
            return None
        out.append(ev)
    return out


def _replays_clean(context: Mapping[str, Any], events: list[dict[str, Any]]) -> bool:
    from orch.model import replay

    from .verifier import CryptoVerifier

    st = replay(
        events,
        {},
        verifier=CryptoVerifier(),
        now=context["now"],
        expected_workspace_id=context["workspace_id"],
        expected_genesis=context["genesis"],
    )
    return not st.chain_errors and not st.workspace.invalid


def _run_log(corpus: Mapping[str, Any]) -> list[dict[str, Any]]:
    part = corpus["log"]
    context, fixture = part["context"], list(part["fixture"])
    if context.get("log") != "workspace":
        raise ValueError("the log variant is a workspace log")
    base = _log_events(context, fixture)
    if base is None or not _replays_clean(context, base):
        raise ValueError("the KAT fixture does not replay cleanly under its pinned context")
    results = []
    for case in part["cases"]:
        e = case["event"]
        events = _log_events(context, [*fixture, e])
        ok = events is not None and _replays_clean(context, events)
        results.append({"head": canon.event_head(e), "accepted": ok, "auth": events[-1]["auth"] if ok else None})
    return results


def _run_view(corpus: Mapping[str, Any]) -> list[dict[str, Any]]:
    part = corpus["view"]
    if tuple(part["views"]) != READER_VIEWS_1 or [c["view"] for c in part["cases"][::3]] != list(READER_VIEWS_1):
        raise ValueError(f"the view variant covers exactly {READER_VIEWS_1}, three cases each")
    if len(part["cases"]) != 3 * len(READER_VIEWS_1):
        raise ValueError("three cases per view")
    results = []
    for case in part["cases"]:
        try:
            auth: str | None = parse_signer(case["view"], case["object"])
        except ValueError:
            auth = None
        results.append({"view": case["view"], "accepted": auth is not None, "auth": auth})
    return results


def run_kat(corpus: Mapping[str, Any], surface: str) -> tuple[list[dict[str, Any]], str]:
    """Run ``reader_kat_1`` (``corpus``, the vector object) for ``surface`` with core's PRODUCTION parsers and return
    ``(results, digest)``: ``log`` reads each case after the fixture with :func:`orch.store.logs.parse_line` and
    replays it with :class:`orch.identity.CryptoVerifier` under the corpus's pinned context; ``view`` parses each case
    with :func:`parse_signer`. A corpus that is not well formed (a fixture that does not replay, a missing view)
    raises :class:`ValueError`. A build declares a level only when the digest equals :data:`KAT_DIGESTS` (9.2)."""
    if surface == "log":
        results = _run_log(corpus)
    elif surface == "view":
        results = _run_view(corpus)
    else:
        raise ValueError(f"surface must be one of {SURFACES}")
    return results, kat_digest(results)
