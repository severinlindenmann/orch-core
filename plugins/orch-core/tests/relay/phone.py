"""The reference test phone (orch-v2-host 11A.15.11 "CI", 11A.15.14; protocol ``orch-relay/docs/protocol-v2.md`` §8.3,
§8.4, §7.2, §7.3, §8.1, §12.2).

**Test tree only.** It is the *device* side of ``dev-pair``'s pairing and of R5/R6's T2: it parses the pairing link
strictly, joins (the pair request with a level-1 ``reader_decl``, the SAS, the primary's challenge and ``cert_code``,
the ``approved`` join anchor), logs in, fetches the member list and its WK grant, then reads (column R ``whoami``) and
writes (one ``relay.submit`` ``answer``) through the bridge. Every check the protocol gives a device runs here, in the
protocol's order, and a failure is a :class:`PhoneError` with the protocol's reason code; the phone never trusts a
field because the host sent it.

It is built on the existing kits (``pairkit.Phone`` for the epoch-0 envelopes, ``bridgekit.Phone`` for the bridge
requests and ``open_chunk`` for the answers) and talks through a small :class:`Net` so the same code runs against the
real relay (:class:`HttpNet`) and, in the self-test, against the in-process pair engine and bridge.

**The codes.** With ``--expect-code-from FIFO`` the phone writes the two codes a human would type, one line each:
``<offer_id> <phase> <CODE>`` where ``phase`` is ``sas`` (typed at the host CLI) or ``cert_code`` (compared at the
primary), see :func:`write_code`. ``dev-pair`` types a code only for its own offer and phase.
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import certs, memberlist, pairing, reader
from orch.identity.reader import KAT_DIGESTS
from orch.wsproto import wire

from . import bridgekit, pairkit

__all__ = [
    "Approved",
    "DevPhone",
    "HttpNet",
    "Net",
    "PhoneError",
    "ulid",
    "write_code",
]

PHASES = ("sas", "cert_code")
_TERMINAL = {"rejected", "expired"}


class PhoneError(Exception):
    """A device-side refusal with the protocol's reason code (``not_my_key``, ``challenge_signature``,
    ``below_anchor``, ...). ``alarm`` marks the cases the protocol says raise the alarm."""

    def __init__(self, code: str, message: str = "", *, alarm: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.alarm = code, alarm


def _ms() -> int:
    return time.time_ns() // 1_000_000


# -- the FIFO of codes ------------------------------------------------------------------------------


def write_code(fifo: str | os.PathLike[str], offer_id: str, phase: str, code: str, *, timeout: float = 30.0) -> None:
    """Append ``<offer_id> <phase> <code>`` to the FIFO ``dev-pair`` reads. The path must be a FIFO of this user (it is
    opened without blocking and retried until a reader is there), so a wrong path never creates a file."""
    if phase not in PHASES:
        raise ValueError(phase)
    line = f"{offer_id} {phase} {code}\n".encode("ascii")
    end = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno not in (errno.ENXIO, errno.EINTR) or time.monotonic() >= end:  # ENXIO: no reader yet
                raise
            time.sleep(0.05)
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISFIFO(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError("not a FIFO of this user")
            os.write(fd, line)
            return
        finally:
            os.close(fd)


# -- the network ------------------------------------------------------------------------------------


class Net(Protocol):
    """What the phone needs from a relay: the epoch-0 pair route, a device session, the member list and grants, and the
    bridge. :class:`HttpNet` is the real one."""

    def pair_post(self, offer_id: str, rid: str, envelope: bytes) -> None: ...

    def pair_answers(self, offer_id: str, rid: str, wait_s: float) -> list[bytes]: ...

    def login(self, device_id: str, sign: Callable[[bytes], bytes]) -> None: ...

    def members(self) -> dict[str, Any]: ...

    def grants(self) -> list[dict[str, Any]]: ...

    def bridge_post(self, rid: str, envelope: bytes) -> None: ...

    def bridge_answers(self, rid: str, wait_s: float) -> list[bytes]: ...


class HttpNet:
    """The real relay over plain ``http.client`` (loopback ``http://127.0.0.1:P`` or ``https``); the origin is the
    literal the host signed. ``token`` is the device session after :meth:`login`."""

    def __init__(self, origin: str, workspace_id: str) -> None:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("origin")
        self.origin, self.ws = origin, workspace_id
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, parts.port
        self.token: str | None = None

    def _call(
        self, method: str, path: str, *, body: Any = None, raw: bytes | None = None, auth: bool = False,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:  # fmt: skip
        headers: dict[str, str] = {}
        data: bytes | None = None
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = canon.dumps(body), "application/json"
        if auth:
            if self.token is None:
                raise PhoneError("not_logged_in")
            headers["Authorization"] = f"Bearer {self.token}"
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            out = r.read(8 << 20)
            return r.status, (json.loads(out) if out else None)
        finally:
            conn.close()

    def _ok(self, method: str, path: str, **kw: Any) -> Any:
        status, obj = self._call(method, path, **kw)
        if not 200 <= status < 300:
            code = obj.get("error") if isinstance(obj, dict) else None
            raise PhoneError("relay_refused", f"{method} {path.split('?')[0]} -> {status} {code}")
        return obj

    def pair_post(self, offer_id: str, rid: str, envelope: bytes) -> None:
        self._ok("POST", f"/v2/workspaces/{self.ws}/pair/{offer_id}/{rid}", raw=envelope)

    def pair_answers(self, offer_id: str, rid: str, wait_s: float) -> list[bytes]:
        q = f"wait={max(1, int(wait_s))}"
        obj = self._ok("GET", f"/v2/workspaces/{self.ws}/pair/{offer_id}/{rid}?{q}", timeout=wait_s + 15)
        return [crypto.unb64u(c["envelope"]) for c in obj["chunks"]]

    def login(self, device_id: str, sign: Callable[[bytes], bytes]) -> None:
        ch = crypto.unb64u(self._ok("POST", "/v2/session/challenge", body={})["challenge"])
        sig = sign(wire.auth_bytes(self.origin, ch, "device", device_id))
        body = {"challenge": crypto.b64u(ch), "kind": "device", "id": device_id, "sig": crypto.b64u(sig)}
        obj = self._ok("POST", "/v2/session/login", body=body)
        self.token = obj["token"]

    def members(self) -> dict[str, Any]:
        return self._ok("GET", f"/v2/workspaces/{self.ws}/members", auth=True)["member_list"]

    def grants(self) -> list[dict[str, Any]]:
        return self._ok("GET", f"/v2/workspaces/{self.ws}/grants", auth=True)["grants"]

    def bridge_post(self, rid: str, envelope: bytes) -> None:
        self._ok("POST", f"/v2/workspaces/{self.ws}/bridge/{rid}", raw=envelope, auth=True)

    def push_register(self, registration_id: str, token: bytes, *, env: str = "sandbox", topic: str = "io.severin.orch"
                      ) -> dict[str, Any]:  # fmt: skip
        """``POST /v2/push/register`` (§19.10) as the logged-in device; the relay's answer, or ``PhoneError``."""
        body = {"workspace_id": self.ws, "registration_id": registration_id, "env": env, "topic": topic,
                "token": crypto.b64u(token)}  # fmt: skip
        return self._ok("POST", "/v2/push/register", body=body, auth=True)  # type: ignore[no-any-return]

    def push_status(self, path: str = "/v2/push/register") -> int:
        """The HTTP status of an empty-bodied push call (the 404 of a relay without push)."""
        return self._call("POST", path, body={}, auth=True)[0]

    def bridge_answers(self, rid: str, wait_s: float) -> list[bytes]:
        """Every chunk of the rid's mailbox so far (non-consuming), in order."""
        out: list[bytes] = []
        after: int | None = None
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            wait = max(1, min(5, int(end - time.monotonic())))
            q = f"wait={wait}" if after is None else f"after={after}&wait={wait}"
            obj = self._ok("GET", f"/v2/workspaces/{self.ws}/bridge/{rid}?{q}", auth=True, timeout=wait + 15)
            for ch in obj["chunks"]:
                out.append(crypto.unb64u(ch["envelope"]))
                after = ch["idx"]
                if ch["last"]:
                    return out
        return out


# -- the pairing result -----------------------------------------------------------------------------


@dataclass
class Approved:
    """What the ``approved`` answer gave, after every §8.3 check: the join floor the phone persists."""

    scopes: tuple[str, ...]
    cert: dict[str, Any]
    pk_pub: bytes
    epoch: int
    list_seq: int
    card_seq: int
    baseline: dict[str, Any] | None


@dataclass
class _Attempt:
    state: str = "new"
    sas: str | None = None
    cert_code: str | None = None
    challenge_request: str | None = None
    wsk_pub: bytes | None = None
    primary_label: str | None = None
    codes_sent: set[str] = field(default_factory=set)


# -- the phone --------------------------------------------------------------------------------------


class DevPhone(pairkit.Phone):
    """One pairing attempt of a phone with fresh keys (a phone without a certificate must draw fresh keys per attempt,
    §8.3), then its life as a member. ``level`` is the reader level it declares (0: no ``reader_decl``)."""

    def __init__(
        self, link: str, *, label: str = "dev-pair test phone", level: int = 1, now_ms: Callable[[], int] = _ms,
        auth: str = "passphrase", **kw: Any,
    ) -> None:  # fmt: skip
        super().__init__(link, **kw)
        self.parsed = pairing.parse_pair_link(link)
        self.pk_pin = self.parsed.fields.pk_pin
        self.label, self.level, self._now, self.auth = label, level, now_ms, auth
        self.a = _Attempt()
        self.approved: Approved | None = None
        self.wk: dict[int, bytes] = {}
        self.pk_pub: bytes | None = None
        self.net_origin = self.parsed.origin
        self._decl: dict[str, Any] | None = None
        self._epoch = 0
        self.scopes: tuple[str, ...] = ()
        self._bridge: bridgekit.Phone | None = None

    # -- the request --------------------------------------------------------------------------------

    def reader_decl(self) -> dict[str, Any] | None:
        """The level-``level`` declaration of §8.3 step 2: ``who`` the link's ``pk_pin`` (a phone without a
        certificate), ``bind`` this offer and attempt, the build's ``view`` KAT digest, signed by the new ``dk_sig``."""
        if self.level < 1:
            return None
        if self._decl is None:
            o = {
                "v": 1, "kind": "reader_decl", "suite": crypto.SUITE_ID, "workspace_id": self.ws,
                "device_id": self.device_id, "dk_hash": crypto.b64u(crypto.dk_hash(self.sig_pub, self.kx_pub)),
                "who": {"pk_pin": crypto.b64u(self.pk_pin)}, "reader": self.level,
                "kat": {str(k): {"view": KAT_DIGESTS[str(k)]["view"]} for k in range(1, self.level + 1)},
                "bind": {"via": "pair", "offer_id": self.offer, "attempt": self.attempt}, "created_ms": self._now(),
            }  # fmt: skip
            sig = crypto.sign(self.sig, reader.LABEL + canon.cj_checked(o))
            self._decl = {"o": o, "sig": crypto.b64u(sig)}
        return self._decl

    def meta(self, *, label: str | None = None, cert: Any = None, **extra: Any) -> dict[str, Any]:  # type: ignore[override]
        decl = self.reader_decl()
        if decl is not None and "reader_decl" not in extra:
            extra["reader_decl"] = decl
        return super().meta(label=label or self.label, cert=cert, **extra)

    # -- the answers (§8.3 step 4 and below) --------------------------------------------------------

    def check(self, env: bytes) -> dict[str, Any]:
        """Step 4's checks in order (shape, ``K_pair`` tag, ``wsk_pin``, the signature), then the state's own."""
        try:
            meta = self.open_answer(env)
        except AssertionError as exc:
            raise PhoneError("not_a_pairing_answer", str(exc)[:80]) from None
        except Exception as exc:  # noqa: BLE001 - a wire/crypto refusal is a dropped answer
            raise PhoneError("not_a_pairing_answer", type(exc).__name__) from None
        self.a.wsk_pub = crypto.unb64u(meta["wsk_pub"])
        state = meta.get("state")
        if state == "pending":
            self._pending(meta)
        elif state == "cert_pending":
            self._cert_pending(meta)
        elif state == "approved":
            self._approved(meta)
        elif state in _TERMINAL:
            self.a.state = state
        else:
            raise PhoneError("not_a_pairing_answer", f"state {state!r}")
        return meta

    def _pending(self, meta: dict[str, Any]) -> None:
        if set(meta) != {"state", "wsk_pub", "sas_nonce", "dk_sig_pub", "dk_kx_pub"}:
            raise PhoneError("not_a_pairing_answer", "pending field set")
        if crypto.unb64u(meta["dk_sig_pub"]) != self.sig_pub or crypto.unb64u(meta["dk_kx_pub"]) != self.kx_pub:
            raise PhoneError("not_my_key", "someone is pairing another key with this link", alarm=True)
        assert self.a.wsk_pub is not None
        self.a.sas = pairing.sas(
            self.ws, self.offer, self.sig_pub, self.kx_pub, self.a.wsk_pub, crypto.unb64u(meta["sas_nonce"], 32)
        )
        if self.a.state in ("new", "pending"):
            self.a.state = "pending"

    def _cert_pending(self, meta: dict[str, Any]) -> None:
        if set(meta) != {"state", "primary_label", "pk_pub", "challenge", "wsk_pub"}:
            raise PhoneError("not_a_pairing_answer", "cert_pending field set")
        pk_pub = crypto.unb64u(meta["pk_pub"])
        if crypto.pk_pin(pk_pub) != self.pk_pin:
            raise PhoneError("pk_pin", "cert_pending names a PK that is not the link's")
        self.pk_pub = pk_pub
        self.a.state = "cert_pending"
        self.a.primary_label = pairing.clean_label(str(meta["primary_label"]))
        if meta["challenge"] is not None:
            self.check_challenge(meta["challenge"], pk_pub)

    def check_challenge(self, signed: Any, pk_pub: bytes) -> str:
        """§8.4 step 5 in order; returns the challenge's ``cert_code`` and keeps it."""
        if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["o"]) is not dict:
            raise PhoneError("challenge_signature", "shape")
        o = signed["o"]
        if set(o) != pairing.CHALLENGE_FIELDS:
            raise PhoneError("challenge_signature", "field set")
        try:
            ok = crypto.verify(pk_pub, crypto.unb64u(signed["sig"]), L["sig_cert_challenge"] + canon.dumps(o))
        except (crypto.CryptoError, crypto.EncodingError, ValueError):
            ok = False
        if not ok:
            raise PhoneError("challenge_signature", "not signed by the pinned PK")
        if o["workspace_id"] != self.ws or o["person_id"] != crypto.hex_id(crypto.person_id(pk_pub)):
            raise PhoneError("not_for_this_workspace")
        mine = (
            o["device_id"] == self.device_id
            and crypto.unb64u(o["dk_sig_pub"]) == self.sig_pub
            and crypto.unb64u(o["dk_kx_pub"]) == self.kx_pub
        )
        if not mine:
            if self._now() >= o["expires_ms"]:
                raise PhoneError("challenge_expired")
            raise PhoneError("not_my_key", "the primary was asked to certify another key", alarm=True)
        if o["attempt"] != self.attempt or o["offer_id"] != self.offer:
            raise PhoneError("not_for_this_pairing")
        if self.a.challenge_request not in (None, o["request_id"]):
            raise PhoneError("not_for_this_pairing", "another request than the accepted one")
        if self._now() >= o["expires_ms"]:
            raise PhoneError("challenge_expired")
        self.a.challenge_request = o["request_id"]
        self.a.cert_code = pairing.cert_code(o)
        return self.a.cert_code

    def _approved(self, meta: dict[str, Any]) -> None:
        want = {"state", "scopes", "cert", "pk_pub", "epoch", "list_seq", "card_seq", "baseline", "wsk_pub"}
        if set(meta) != want:
            raise PhoneError("not_a_pairing_answer", "approved field set")
        pk_pub = crypto.unb64u(meta["pk_pub"])
        if crypto.pk_pin(pk_pub) != self.pk_pin:
            raise PhoneError("pk_pin", "approved names a PK that is not the link's")
        try:
            o = certs.verify_cert(meta["cert"], pk_pub, self._now())
        except Exception as exc:  # noqa: BLE001 - the cert refusal codes (cert_invalid, cert_expired, revoked)
            raise PhoneError(getattr(exc, "code", "cert_invalid"), "the certificate") from None
        if (
            o["device_id"] != self.device_id
            or crypto.unb64u(o["dk_sig_pub"]) != self.sig_pub
            or crypto.unb64u(o["dk_kx_pub"]) != self.kx_pub
        ):
            raise PhoneError("cert_invalid", "the certificate is not for this device's two keys")
        scopes = meta["scopes"]
        if not isinstance(scopes, list) or not scopes or not set(scopes) <= set(o["scopes_max"]):
            raise PhoneError("scopes", "scopes exceed the certificate")
        epoch, ls, cs, base = meta["epoch"], meta["list_seq"], meta["card_seq"], meta["baseline"]
        safe = (1 << 53) - 1
        ok_int = lambda v, lo, hi: type(v) is int and lo <= v <= hi  # noqa: E731
        base_ok = base is None or (
            isinstance(base, dict) and set(base) == {"n", "hash"} and ok_int(base["n"], 0, safe)
            and isinstance(base["hash"], str) and len(base["hash"]) == 71 and base["hash"].startswith("sha256:")
        )  # fmt: skip
        if not (ok_int(epoch, 1, (1 << 32) - 1) and ok_int(ls, 1, safe) and ok_int(cs, 1, safe) and base_ok):
            raise PhoneError("not_a_pairing_answer", "join anchor")
        self.pk_pub = pk_pub
        self.approved = Approved(tuple(scopes), meta["cert"], pk_pub, epoch, ls, cs, base)
        self.a.state = "approved"

    # -- the pairing loop ---------------------------------------------------------------------------

    def pair(
        self,
        net: Net,
        *,
        codes: Callable[[str, str, str], None],
        deadline_s: float = 600.0,
        poll_s: float = 1.0,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Approved:
        """Send the pair request, then poll ``pair_status`` until ``approved``. ``codes(offer_id, phase, code)`` is told
        the SAS (``sas``) once the signed ``pending`` answer is checked, and the ``cert_code`` once the primary's
        challenge is checked; both are shown to the human, who types them where ``dev-pair`` asks (11A.15.11 step 8).
        Raises :class:`PhoneError` on a refusal or a terminal ``rejected``/``expired``."""
        clock = now_ms or self._now
        end = time.monotonic() + deadline_s
        env, rid = self.envelope(self.meta(), ts=clock())
        net.pair_post(self.offer, rid, env)
        sent: set[str] = set()
        while time.monotonic() < end:
            for answer in net.pair_answers(self.offer, rid, min(5.0, poll_s * 5)):
                self.check(answer)
            if self.a.sas and "sas" not in sent:
                sent.add("sas")
                codes(self.offer, "sas", self.a.sas)
            if self.a.cert_code and "cert_code" not in sent:
                sent.add("cert_code")
                codes(self.offer, "cert_code", self.a.cert_code)
            if self.a.state == "approved":
                assert self.approved is not None
                return self.approved
            if self.a.state in _TERMINAL:
                raise PhoneError(self.a.state, "the pairing ended")
            sleep(poll_s)
            env, rid = self.status(ts=clock())
            net.pair_post(self.offer, rid, env)
        raise PhoneError("timeout", f"no approval within {deadline_s:.0f} s (state {self.a.state})")

    # -- the join (§8.3 "On approved", §7.2, §7.3, §12.2) -------------------------------------------

    def join(self, net: Net) -> dict[int, bytes]:
        """Log in, fetch the member list and the grant, check them against the join floor and the list's ``wk_commit``;
        returns ``{epoch: WK}``."""
        ap = self.approved
        if ap is None:
            raise PhoneError("not_approved")
        net.login(self.device_id, lambda payload: crypto.sign(self.sig, payload))
        assert self.a.wsk_pub is not None
        try:
            ml = memberlist.check_member_list(net.members(), self.a.wsk_pub, self.ws)
        except Exception as exc:  # noqa: BLE001 - the list's refusal code
            raise PhoneError(getattr(exc, "code", "member_list"), "the member list") from None
        if ml.epoch < ap.epoch or ml.list_seq < ap.list_seq:
            raise PhoneError("below_anchor")
        me = next((m for m in ml.members if m.device_id == self.device_id), None)
        if me is None:
            raise PhoneError("not_a_member")
        if ml.list_seq == ap.list_seq and sorted(me.scopes) != sorted(ap.scopes):
            raise PhoneError("scopes_mismatch")
        grants = [g for g in net.grants() if g.get("o", {}).get("epoch") == ml.epoch]
        if not grants:
            raise PhoneError("no_grant", f"no grant for epoch {ml.epoch}")
        for g in grants:
            grant = memberlist.check_wk_grant(g, self.a.wsk_pub, self.ws, self.device_id)
            wk = crypto.open_sealed(
                self.kx, bytes.fromhex(self.device_id), "wk", bytes.fromhex(self.ws), grant.epoch, grant.sealed
            )
            if grant.epoch == ml.epoch and pairing.wk_commit(self.ws, ml.epoch, wk) != ml.wk_commit:
                raise PhoneError("wk_commit", "the opened key is not the list's commitment")
            self.wk[grant.epoch] = wk
        if ml.epoch not in self.wk:
            raise PhoneError("no_grant", "the current epoch's grant did not open")
        self._epoch = ml.epoch
        self.scopes = tuple(me.scopes)
        return dict(self.wk)

    # -- column R (§8.1, §8.8, 11A.14) --------------------------------------------------------------

    def _bridge_phone(self) -> bridgekit.Phone:
        """The bridge kit's request builder over this phone's device key and certificate (no person key: events are
        signed by the device, §8.7)."""
        if self._bridge is None:
            assert self.approved is not None
            bp = bridgekit.Phone.__new__(bridgekit.Phone)
            bp.pk = None  # type: ignore[assignment]
            bp.dk = self.sig
            bp.pk_pub, bp.dk_pub = self.approved.pk_pub, self.sig_pub
            bp.cert = self.approved.cert
            bp.device, bp.person = self.device_id, self.approved.cert["o"]["person_id"]
            bp.seq = 0
            self._bridge = bp
        return self._bridge

    def rd(self) -> str | None:
        decl = self.reader_decl()
        return None if decl is None else reader.decl_digest(decl)

    def call(
        self, net: Net, meta: dict[str, Any], *, wait_s: float = 30.0, ts_ms: int | None = None,
        epoch: int | None = None,
    ) -> tuple[dict[str, Any], Any]:  # fmt: skip
        """One column-R request: sealed under the current WK, signed by the device; the answer's chunks are checked
        (WSK signature, tag) and returned as ``(meta of chunk 0, decoded data)``."""
        bp = self._bridge_phone()
        if self.level >= 1 and "rd" not in meta:
            meta = {**meta, "rd": self.rd()}
        env = bp.request(self.wk, meta, epoch=epoch if epoch is not None else self._epoch,
                         ts_ms=ts_ms if ts_ms is not None else self._now(), ws=self.ws)  # fmt: skip
        rid = wire.parse_header(env[: wire.HEADER_LEN]).rid.hex()
        net.bridge_post(rid, env)
        assert self.a.wsk_pub is not None
        metas, data = [], b""
        for chunk in net.bridge_answers(rid, wait_s):
            _, m, d = bridgekit.open_chunk(chunk, self.wk, self.a.wsk_pub)
            metas.append(m)
            data += d
        if not metas:
            raise PhoneError("no_answer", f"no answer to {meta.get('op')} within {wait_s:.0f} s")
        return metas[0], (json.loads(data) if data else None)

    def whoami(self, net: Net, **kw: Any) -> tuple[dict[str, Any], Any]:
        return self.call(net, {"op": "whoami", "args": {}}, **kw)

    def answer_event(
        self, log: str, question: str, qhash: str, option: str, *, based_on: str, auth: str | None = None
    ) -> dict[str, Any]:
        """The device-signed ``question.answered`` event. ``question``/``qhash`` name the question (``Q<n>`` and its
        hash from ``ticket.get``), ``based_on`` the ticket head the person saw (a ``sha256:`` hash, never null for an
        answer). ``auth`` defaults to the phone's: ``passphrase`` for a test phone; ``convenience`` only for the
        Simulator dev path (11A.17)."""
        bp = self._bridge_phone()
        ev = bp.event(
            log, "question.answered", eid=bridgekit.ulid(self._now()), question=question, hash=qhash, option=option,
            based_on=based_on,
        )  # fmt: skip
        ev["auth"] = auth or self.auth
        ev.pop("sig")
        ev["sig"] = crypto.b64u(crypto.sign(self.sig, canon.person_signing_bytes(self.ws, log, ev)))
        return ev

    def answer(
        self, net: Net, log: str, question: str, qhash: str, option: str, *, based_on: str, auth: str | None = None,
        wait_s: float = 30.0,
    ) -> tuple[dict[str, Any], Any]:  # fmt: skip
        """One ``relay.submit`` ``answer`` write; the idem is the event id (11A.4)."""
        ev = self.answer_event(log, question, qhash, option, based_on=based_on, auth=auth)
        meta = {"op": "answer", "args": {"log": log, "events": [ev]}, "idem": ev["id"]}
        return self.call(net, meta, wait_s=wait_s)


def ulid() -> str:
    """A fresh ULID for a log or event id (the bridge kit's, at the current time)."""
    return bridgekit.ulid(_ms())
