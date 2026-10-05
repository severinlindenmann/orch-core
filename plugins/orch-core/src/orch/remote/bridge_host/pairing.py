"""Pairing a browser or phone (spec §8.1, §8.2, §9.2): offers, the op=pair checks, the pending devices and their
credential registration. Offers and pending pairings live in the host's memory only: the pairing secret is shown
once in the link and never written anywhere.

Refusals here come before any registered device's signature verified, so they spend a budget: the host-wide one, or,
once an open offer is known, that offer's own (keyed by its unguessable pairing id), so junk envelopes cannot starve
an honest pairing. A spent budget means the refusal is dropped.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Callable

from orch.remote.bridge_host.assertion import CHALLENGE_MS, registration_challenge, verify_registration
from orch.remote.bridge_host.budgets import SlidingLimit, offer_budget
from orch.remote.bridge_host.envelope import Header, b64u, unb64u, unhex
from orch.remote.bridge_host.keys import device_fingerprint, device_id, host_pin, pair_mac, phone_link_proof
from orch.remote.bridge_host.outcome import Verdict, drop, refuse
from orch.remote.bridge_host.registry import SCOPES, Credential, Device, Registry
from orch.remote.bridge_host.shown import clean_shown
from orch.remote.bridge_host.signatures import signed_bytes, verify

OFFER_MS = 600_000  # 10 minutes, single use
WINDOW_MS = 300_000
LABEL_MAX = 80


@dataclass
class Offer:
    pairing_id: bytes
    secret: bytes
    scope: str
    expires_ms: int
    used: bool = False
    budget: SlidingLimit = field(default_factory=offer_budget)

    def __repr__(self) -> str:  # never print the secret
        return f"Offer(pairing_id={self.pairing_id.hex()!r}, scope={self.scope!r}, used={self.used!r})"


@dataclass
class Pending:
    pub: bytes
    pairing_id: str
    scope: str
    phone_link: str | None
    label: str
    state: str = "pending"  # pending | approved | rejected
    registration: tuple[bytes, int] | None = None  # the open registration challenge and its expiry
    credential: Credential | None = None

    @property
    def fingerprint(self) -> str:
        return device_fingerprint(self.pub)


@dataclass
class Pairing:
    """One workspace's offers (by pairing id hex) and pending pairings (by device id hex)."""
    workspace: bytes
    offers: dict[str, Offer] = field(default_factory=dict)
    pending: dict[str, Pending] = field(default_factory=dict)

    # -- the Remote tab ------------------------------------------------------------------------------------------------

    def offer(self, scope: str, now_ms: int, rand: Callable[[int], bytes], host_pub: bytes) -> tuple[Offer, str]:
        """A new offer and its link fragment `v1.<workspace>.<pairing id>.<b64u S>.<b64u host pin>`. The fragment is
        a secret for 10 minutes: show it once, never log it."""
        if scope not in SCOPES:
            raise ValueError("unknown scope")
        self.offers = {k: o for k, o in self.offers.items() if now_ms < o.expires_ms}
        o = Offer(rand(16), rand(32), scope, now_ms + OFFER_MS)
        self.offers[o.pairing_id.hex()] = o
        return o, f"v1.{self.workspace.hex()}.{o.pairing_id.hex()}.{b64u(o.secret)}.{b64u(host_pin(host_pub))}"

    def approve(self, did: str, registry: Registry, now_ms: int, scope: str | None = None) -> Device:
        """The owner approved after comparing fingerprints; the scope may only be lowered. Writes and audits the
        registry entry."""
        p = self.pending.get(did)
        if p is None or p.state != "pending":
            raise LookupError("no pending pairing for this device")
        scope = scope or p.scope
        if scope not in SCOPES or SCOPES[scope] > SCOPES[p.scope]:
            raise ValueError("a pairing's scope can only be lowered")
        dev = registry.add(Device(did, p.pub, scope, p.label, now_ms, False, p.phone_link, p.credential), now_ms)
        p.state = "approved"
        return dev

    def reject(self, did: str, registry: Registry, now_ms: int) -> None:
        p = self.pending.get(did)
        if p is None or p.state != "pending":
            raise LookupError("no pending pairing for this device")
        p.state = "rejected"
        registry.audit(now_ms, "pairing_rejected", device=did)

    # -- requests from a device that is not registered (§6.1 step 3) ----------------------------------------------------

    def pair(self, h: Header, hb: bytes, body: bytes, sig: bytes, meta: dict, now_ms: int, host_budget: SlidingLimit,
             phone_key: Callable[[str], bytes | None]) -> Verdict:
        """§8.1 step 3, in its order."""
        def unverified(code, bucket=host_budget, **extra):
            return refuse(code, **extra) if bucket.take(now_ms) else drop("budget")

        try:
            pid = meta["pairing_id"]
            unhex(pid, 16)
            pub, mac = unhex(meta["pub"], 65), unhex(meta["mac"], 32)
            label = clean_shown(meta.get("label", ""))[:LABEL_MAX]
        except (KeyError, TypeError, ValueError):
            return unverified("malformed")
        did = h.device.hex()
        offer = self.offers.get(pid)
        held = self.pending.get(did)
        resend = held is not None and held.pairing_id == pid and held.pub == pub  # its `pending` answer was lost
        if offer is None or now_ms >= offer.expires_ms or (offer.used and not resend):
            return unverified("pairing_closed")
        if device_id(h.workspace, pub) != h.device or not verify(pub, sig, signed_bytes(hb, body)):
            return unverified("bad_signature", offer.budget)
        if not hmac.compare_digest(pair_mac(offer.secret, h.workspace, offer.pairing_id, pub), mac):
            return unverified("pairing_closed", offer.budget)  # the same answer as no offer: the link is the secret
        if abs(now_ms - h.ts_ms) > WINDOW_MS:
            return unverified("stale_timestamp", offer.budget, host_ms=now_ms)
        if resend:  # nothing changes: the same answer again
            return self._pending_answer(did, held)
        offer.used = True
        link = None  # a phone link is recorded only with its proof (§8.2)
        phone_id = meta.get("phone_id")
        key = phone_key(phone_id) if isinstance(phone_id, str) and phone_id else None
        if key is not None:
            try:
                proof = unhex(meta.get("phone_proof"), 32)
            except ValueError:
                proof = b""
            if hmac.compare_digest(phone_link_proof(key, h.device), proof):
                link = phone_id
        self.pending[did] = Pending(pub, pid, offer.scope, link, label)
        return self._pending_answer(did, self.pending[did])

    @staticmethod
    def _pending_answer(did: str, p: Pending) -> Verdict:
        return Verdict("pair_pending", device=did, scope=p.scope,
                       fields={"fingerprint": p.fingerprint, "device": did, "scope": p.scope, "phone_link": p.phone_link})

    def pending_request(self, h: Header, hb: bytes, body: bytes, sig: bytes, meta: dict, now_ms: int,
                        host_budget: SlidingLimit, rand: Callable[[int], bytes], rp_id: str, origin: str) -> Verdict | None:
        """pair_status, credential_begin and credential_finish from a device id with a pending pairing, verified with
        the pending key; None when the request is not one of these (the caller refuses it not_paired)."""
        did = h.device.hex()
        p = self.pending.get(did)
        op = meta.get("op")
        if p is None or op not in ("pair_status", "credential_begin", "credential_finish"):
            return None

        def unverified(code, **extra):
            return refuse(code, **extra) if host_budget.take(now_ms) else drop("budget")

        if not verify(p.pub, sig, signed_bytes(hb, body)):
            return unverified("bad_signature")
        if abs(now_ms - h.ts_ms) > WINDOW_MS:
            return unverified("stale_timestamp", host_ms=now_ms)
        if op == "pair_status":  # read-only: a replay changes nothing
            return Verdict("pair_status", device=did, fields={"state": p.state})
        if p.state != "pending":
            return unverified("pairing_closed")
        if op == "credential_begin":
            nonce, expires = rand(32), now_ms + CHALLENGE_MS
            p.registration = (registration_challenge(h.workspace, h.device, expires, nonce), expires)
            return Verdict("credential_begin", device=did, fields={"nonce": nonce.hex(), "expires_ms": expires})
        reg, p.registration = p.registration, None  # single use, whatever happens next
        if reg is None or now_ms >= reg[1] or p.credential is not None:
            return unverified("assertion_failed", why="no_open_registration")
        try:
            cid, att, cdj = (unb64u(meta[k]) for k in ("credential_id", "attestation_object", "client_data_json"))
        except (KeyError, TypeError, ValueError):
            return unverified("malformed")
        cred, why = verify_registration(reg[0], cid, att, cdj, rp_id, origin)
        if cred is None:
            return unverified("assertion_failed", why=why or "registration")
        p.credential = cred
        return Verdict("credential_finish", device=did, fields={"registered": True, "synced": cred.be})
