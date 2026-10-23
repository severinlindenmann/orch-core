"""The test owner client of ``dev-pair`` and R5's T2 (orch-v2-host 11A.15.11 "The sandbox identity", 11A.15.14 step 4).

**Test tree only, never packaged.** It plays the owner's *primary* for a throwaway person whose recovery code sits in a
dev-pair sandbox (``<root>/secrets/recovery-code``): it creates the owner's relay account, makes the primary's
certificate for ``orch host relay primary set``, and works the primary's cert-request queue for ``orch host relay pair
--primary external`` (challenge, ``cert_code`` comparison, certificate or rejection). It refuses to run unless the
sandbox manifest ``<root>/.orch-dev-sandbox`` names that very root (:func:`orch.devsandbox.check_manifest`,
accident prevention, not containment) and the manifest names the sandbox person.

**Shared code (N6).** Everything with a security rule is the product's: :class:`orch.relay_owner.OwnerClient` (routes,
login, account), :class:`orch.relay_owner.PrimaryQueue` (``check_cert_request``, ``open_cert_request``, the journal, the
one-signature-per-request rule) and :func:`orch.relay_owner.primary_certificate`. Only the person-key source
(:class:`tests.relay.personkey_sandbox.SandboxPersonKey`, the code from the sandbox) and the packaging differ.

**Files** (all 0600, under ``<root>/secrets`` which must be a 0700 directory of this user):
``primary-sig.key`` and ``primary-kx.key`` (the primary's two device keys, 32-byte scalars in hex, made on first use);
``<root>/primary-cert.json`` (the primary's certificate, public). The primary's request journal lives in
``<root>/owner-state`` (``person/primary/requests``).

The card the primary checks requests against is the one the *host* stored when it linked (``hosts/<ws>/membership/
cards/card-<n>.json`` under the sandbox state), verified under the sandbox person's own key (``wscard.verify_card``),
never one a relay served.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.crypto.labels import L
from orch.identity import wscard
from orch.identity.reader import KAT_DIGESTS
from orch.net import http as rhttp
from orch.relay_owner import (
    ANSWER_LABELS,
    OwnerClient,
    PrimaryJournal,
    PrimaryQueue,
    journal_dir,
    primary_certificate,
)
from orch.relay_owner import inline as product_inline
from orch.relay_owner.inline import InlineRefused, PairOutcome

from . import personkey_sandbox as sandbox

__all__ = ["OwnerRefusedError", "PairOutcome", "PrimaryKeys", "SandboxOwner"]

SECRETS = "secrets"
SIG_FILE, KX_FILE = "primary-sig.key", "primary-kx.key"
CERT_FILE = "primary-cert.json"
CERT_FILE_TMP = "primary-cert.json.tmp"  # a name dev-pair lists in the manifest
LABEL = "orch dev sandbox primary"
_SCALAR = re.compile(r"[0-9a-f]{64}")


class OwnerRefusedError(Exception):
    """The owner client refused locally (not a sandbox, a wrong code, no card); ``code`` is a short token."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _ms() -> int:
    return time.time_ns() // 1_000_000


# -- the primary's device keys ----------


def _secrets_fd(root: Path) -> int:
    """``<root>/secrets`` as a directory descriptor: a 0700 directory of this user, opened without following a link."""
    try:
        fd = os.open(root / SECRETS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise OwnerRefusedError("sandbox", "no secrets directory in the sandbox") from None
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise OwnerRefusedError("sandbox", "secrets/ is not a 0700 directory of this user")
    return fd


def _scalar_file(dir_fd: int, name: str) -> int:
    """The private scalar in ``name`` (hex), made on first use with ``O_EXCL`` (never overwritten)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        key = crypto.generate_private_key()
        wfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        try:
            os.write(wfd, crypto.private_scalar(key).hex().encode())
        finally:
            os.close(wfd)
        fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise OwnerRefusedError("sandbox", f"{name} is not a 0600 file of this user")
        raw = os.read(fd, 128).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    if not _SCALAR.fullmatch(raw):
        raise OwnerRefusedError("sandbox", f"{name} is not a key")
    return int(raw, 16)


class PrimaryKeys:
    """The primary's device keys (``sig`` signs relay logins only; ``kx`` opens cert requests): a
    :class:`orch.relay_owner.DeviceSigner` plus the ``cert-request`` unseal."""

    def __init__(self, root: Path) -> None:
        fd = _secrets_fd(root)
        try:
            self._sig = crypto.private_key_from_scalar(_scalar_file(fd, SIG_FILE))
            self._kx = crypto.private_key_from_scalar(_scalar_file(fd, KX_FILE))
        finally:
            os.close(fd)
        self.dk_sig_pub = crypto.public_bytes(self._sig)
        self.dk_kx_pub = crypto.public_bytes(self._kx)
        self.device_id = crypto.device_id(self.dk_sig_pub)

    def __repr__(self) -> str:
        return f"PrimaryKeys(device={self.device_id.hex()})"

    def sign(self, payload: bytes) -> bytes:
        if not payload.startswith(L["sig_relay_auth"]):
            raise OwnerRefusedError("label", "the primary's device key signs relay logins only")
        return crypto.sign(self._sig, payload)

    def sign_decl(self, o: dict[str, Any]) -> bytes:
        """The device key's signature over a ``reader_decl`` object (its own label, never a login's)."""
        from orch.identity.reader import LABEL as DECL_LABEL

        return crypto.sign(self._sig, DECL_LABEL + canon.cj_checked(o))

    def unseal(self, object_id: bytes, sealed: bytes) -> bytes:
        return crypto.open_sealed(self._kx, self.device_id, "cert-request", object_id, 0, sealed)


# -- the owner ----------


class SandboxOwner:
    """The throwaway owner of one sandbox, against one relay origin.

    ``origin`` is the literal the host signs (``link.json``'s). ``transport`` is a test seam
    (:class:`orch.net.http.Transport`); the default is a connection to ``origin``. ``kdf_n`` is the low-N seam for
    fast unit tests of the recovery-code derivation."""

    def __init__(self, root: str | os.PathLike[str], origin: str, *, transport: rhttp.Transport | None = None,
                 kdf_n: int | None = None, now_ms: Callable[[], int] = _ms) -> None:  # fmt: skip
        self.root = Path(root)
        try:
            self.manifest = sandbox.check_manifest(self.root)
        except sandbox.SandboxRefused as exc:
            raise OwnerRefusedError("sandbox", f"not a dev-pair sandbox: {exc}") from None
        person = self.manifest.get("person_id")
        if type(person) is not str:
            raise OwnerRefusedError("sandbox", "the manifest names no sandbox person yet (run after orch init)")
        self.person_id = bytes.fromhex(person[2:])
        self.origin = origin
        self._transport, self._kdf_n, self._now = transport, kdf_n, now_ms
        self.keys = PrimaryKeys(self.root)
        self._client: OwnerClient | None = None
        self.state = self.root / "owner-state"

    # -- plumbing ----------

    @property
    def client(self) -> OwnerClient:
        if self._client is None:
            self._client = OwnerClient(self.origin, self.keys, transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def person_key(self, labels: Any) -> sandbox.SandboxPersonKey:
        """The sandbox person's key for one command: the code read from ``secrets/recovery-code`` after the manifest
        check, matched against the manifest's person before any signature."""
        return sandbox.SandboxPersonKey(self.root, labels=labels, person_id=self.person_id, _kdf_n=self._kdf_n)

    # -- account and the primary's certificate (11A.15.11 step 6) ----------

    def primary_cert(self, *, label: str = LABEL) -> dict[str, Any]:
        """The primary's PK-signed certificate (``scopes_max: ["look"]``)."""
        with self.person_key({"sig_device_cert"}) as pk:
            return primary_certificate(pk, self.keys, self.keys.dk_kx_pub, label=label, created_ms=self._now())

    def create_account(self, invite: str, *, label: str = LABEL) -> Path:
        """``POST /v2/accounts`` with the operator's invite, the sandbox ``PK`` and the primary's certificate;
        writes the certificate to ``<root>/primary-cert.json`` (0600) for ``orch host relay primary set --cert`` and
        returns that path. The invite is a secret: it is not kept."""
        with self.person_key({"sig_device_cert"}) as pk:
            cert = primary_certificate(pk, self.keys, self.keys.dk_kx_pub, label=label, created_ms=self._now())
            person = self.client.create_account(invite, pk.pk_pub, cert)
        if person != self.person_id.hex():
            raise OwnerRefusedError("account", "the relay created another person")
        return self.write_cert(cert)

    def write_cert(self, cert: dict[str, Any]) -> Path:
        path = self.root / CERT_FILE
        tmp = self.root / CERT_FILE_TMP
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, canon.dumps(cert))
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return path

    # -- reader declaration of the primary (§11A.17.4, O4) ----------

    def reader_decl(self, workspace_id: str, *, level: int = 1) -> dict[str, Any]:
        """The primary's level-``level`` ``reader_decl`` from the pure builders (``orch.custody.readerdecl.build``),
        ``via: "enrol"``, signed by the primary's device key. It is what the sandbox primary presents as a reader
        (the gate is exercised, not bypassed, O4)."""
        from orch.custody import readerdecl

        o = readerdecl.build(
            workspace_id=workspace_id, dk_sig_pub=self.keys.dk_sig_pub,
            dk_hash=crypto.b64u(crypto.dk_hash(self.keys.dk_sig_pub, self.keys.dk_kx_pub)),
            person_hex=self.person_id.hex(), via="enrol", created_ms=self._now(), level=level,
            kat={str(k): dict(KAT_DIGESTS[str(k)]) for k in range(1, level + 1)},
        )  # fmt: skip
        return {"o": o, "sig": crypto.b64u(self.keys.sign_decl(o))}

    # -- the primary's queue (11A.15.9 "Inline primary steps", as the external primary) ----------

    def card(self, workspace_id: str, state_dir: str | os.PathLike[str]) -> wscard.VerifiedCard:
        """The host's stored card of ``workspace_id`` (the highest ``card-<n>.json``), verified under the sandbox
        person's key."""
        cards = Path(state_dir) / "hosts" / workspace_id / "membership" / "cards"
        try:
            names = sorted(
                (int(m.group(1)), n) for n in os.listdir(cards) if (m := re.fullmatch(r"card-([1-9][0-9]*)\.json", n))
            )  # fmt: skip
            raw = (cards / names[-1][1]).read_bytes()
        except (OSError, IndexError):
            raise OwnerRefusedError("no_card", "the host has stored no card for this workspace") from None
        with self.person_key(ANSWER_LABELS) as pk:  # the public key is known once the code is read
            try:
                return wscard.verify_card(canon.loads_strict(raw), pk.pk_pub)
            except Exception as exc:  # noqa: BLE001 - any refusal is "not this person's card"
                raise OwnerRefusedError("bad_card", type(exc).__name__) from None

    def serve_request(
        self,
        request_id: str,
        workspace_id: str,
        state_dir: str | os.PathLike[str],
        read_code: Callable[[str, str], str | None],
        *,
        wait_s: float = 120.0,
        poll_wait: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> PairOutcome:
        """Work the one cert request the host reported for the offer with the **product's** step
        (:func:`orch.relay_owner.inline.serve_request`, B1b/N6): wait for it in the queue, check and open it, then sign
        the challenge, and sign the certificate **only if** ``read_code(label, scopes)`` returns the code the phone
        shows (11A.15.11 step 8); anything else rejects. Only the person-key source (the sandbox's file), the
        packaging and the single try (``recovery_tries=1, code_tries=1``: the script types once) differ.

        ``read_code(label, scopes)`` is the caller's prompt or FIFO read: ``label`` is the device's own name for itself
        ("named by the device"), ``scopes`` the requested ones; it returns the typed code or ``None`` (reject)."""
        card = self.card(workspace_id, state_dir)
        journal_dir_ = journal_dir(self.state)
        journal_dir_.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.person_key(ANSWER_LABELS) as pk:
            owner_pk = pk.pk_pub
            q = PrimaryQueue(
                self.client, PrimaryJournal(journal_dir_), pk_pub=owner_pk,
                cards=lambda ws: card if ws == card.workspace_id else None, unseal=self.keys.unseal,
                now_ms=self._now,
            )  # fmt: skip
        try:
            return product_inline.serve_request(
                q, request_id, owner_pk=owner_pk, pk_factory=lambda **kw: self.person_key(kw["labels"]),
                ask_cert_code=lambda _p, pending, _d: read_code(pending.label, ",".join(pending.scopes_max)),
                recovery_tries=1, code_tries=1, wait_s=wait_s, poll_wait=poll_wait, now_ms=self._now, sleep=sleep,
            )  # fmt: skip
        except InlineRefused as exc:
            raise OwnerRefusedError(exc.detail or exc.code, exc.message) from None
