"""The inline primary's request step (orch-v2-host 11A.15.9 "Inline primary steps", 11A.15.23 "As built in B1b").

One function, :func:`serve_request`, works the one cert request R6 reported for an offer. It is the **product**
implementation: ``orch host relay pair --primary inline`` calls it with the terminal, and the test owner client
(``tests/relay/owner.py``, N6) calls the same function with its sandbox person key and FIFO. Order (Opus review
M1, S:8389 step 3):

1. wait until the request is **verified** by the queue (card from the genesis owner, ``check_cert_request``, the kx
   unseal) *without* the person key: ``queue.poll``;
2. bind it to what the host attached (L2): the request's ``offer_id`` is the attached offer, its device is the
   device the host consumed; show the label (own line, bracketed, "named by the device"), the scopes, the short
   device id and the ``client_hosted`` warning;
3. only then **one** person-key source (the recovery code, typed on the terminal; up to ``recovery_tries`` tries,
   each a fresh source, M3) covers the challenge, the typed ``cert_code`` and the signature or the rejection:
   ``cert_code`` is computed from the **queue's own journaled challenge**, never from ``host.relay_pair_show``
   (S:8396 step 4 deviation: the Mac does not display its code, the human types the phone's, compared in constant
   time); input must match ``^[A-Z2-7]{6}$`` after strip and upper, anything else is discarded and nothing is sent
   (M2); up to ``code_tries`` typed tries; empty, exhausted or too late (bounded by the request's ``expires_ms``,
   L1) signs the **rejection** while the key is held. A Ctrl-C leaves the request challenged and nothing signed.

No secret is returned, logged or put in an error: the outcome carries the public certificate or rejection only.
"""

from __future__ import annotations

import hmac
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity import wscard
from orch.identity.pairing import normalize_code

from .queue import ANSWER_LABELS, Pending, PrimaryQueue, QueueRefused

__all__ = [
    "RECOVERY_ACTION",
    "InlineRefused",
    "PairOutcome",
    "normalize_code",
    "serve_request",
    "stored_card",
]

#: shown by the recovery-code prompt; fixed text, never the device's label (M2)
RECOVERY_ACTION = "approve a new device (the phone you are pairing)"
CERT_CODE_PROMPT = "step 2 of 2, compare code: type the 6 characters the phone shows now (empty: reject): "


class InlineRefused(Exception):
    """The step ended without a decision the caller can send. ``code``: ``timeout`` (the request never reached the
    primary), ``refused`` (the queue refused it, ``detail`` the queue's code), ``mismatch`` (the request is not for
    the attached offer or device), ``no_key`` (no valid recovery code in ``recovery_tries`` tries; nothing was
    signed), ``expired`` (the request ended while waiting for the code), ``no_card``, ``bad_card``."""

    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.detail = code, message, detail


@dataclass(frozen=True)
class PairOutcome:
    """What :func:`serve_request` decided: ``cert`` (the signed certificate) or ``rejection`` (the signed rejection)."""

    request_id: str
    cert: dict[str, Any] | None
    cert_code: str | None
    label: str
    scopes: tuple[str, ...]
    rejection: dict[str, Any] | None = None
    device_id: str | None = None

    @property
    def rejected(self) -> bool:
        return self.cert is None


def _ms() -> int:
    return time.time_ns() // 1_000_000


def _short(device_id: str) -> str:
    d = device_id[2:] if device_id.startswith("d_") else device_id
    return "d_" + d[:8] + "…"


def _plain(text: str, cap: int = 80) -> str:
    """Terminal-safe: controls and format characters out, whitespace a space, capped (the label is
    ``clean_label``-ed by the queue already)."""
    from orch.identity.pairing import clean_label

    try:
        cleaned = clean_label(text.replace("\n", " "))
    except Exception:  # noqa: BLE001 - lone surrogates
        return "?"
    kept = "".join(" " if c.isspace() else c for c in cleaned if c.isspace() or c.isprintable())
    return " ".join(kept.split())[:cap]


def stored_card(state_dir: str | os.PathLike[str], workspace_id: str, owner_pk: bytes) -> wscard.VerifiedCard:
    """The host's stored card of ``workspace_id`` (the highest ``card-<n>.json`` under ``hosts/<ws>/membership/cards``),
    verified under the workspace owner's key (``wscard.verify_card``), never one a relay served."""
    cards = Path(state_dir) / "hosts" / workspace_id / "membership" / "cards"
    try:
        names = sorted(
            (int(m.group(1)), n) for n in os.listdir(cards) if (m := re.fullmatch(r"card-([1-9][0-9]*)\.json", n))
        )  # fmt: skip
        raw = (cards / names[-1][1]).read_bytes()
    except (OSError, IndexError):
        raise InlineRefused("no_card", "the host has stored no card for this workspace") from None
    try:
        return wscard.verify_card(canon.loads_strict(raw), owner_pk)
    except Exception as exc:  # noqa: BLE001 - any refusal is "not this person's card"
        raise InlineRefused("bad_card", type(exc).__name__) from None


def serve_request(
    queue: PrimaryQueue,
    request_id: str,
    *,
    pk_factory: Callable[..., Any],
    ask_cert_code: Callable[[str, Pending, float | None], str | None],
    show: Callable[[str], None] = lambda _t: None,
    owner_pk: bytes,
    offer_id: str | None = None,
    device_id: str | None = None,
    recovery_tries: int = 3,
    code_tries: int = 3,
    wait_s: float = 120.0,
    poll_wait: int = 2,
    now_ms: Callable[[], int] = _ms,
    sleep: Callable[[float], None] = time.sleep,
) -> PairOutcome:
    """Work ``request_id`` (module doc). ``pk_factory(labels=, pk_pub=, action=)`` returns the person-key source (a
    context manager); ``ask_cert_code(prompt, pending, deadline_s)`` returns what the person typed or ``None`` (no
    answer in ``deadline_s`` seconds, or a reject); ``show`` writes one line to the person's terminal.
    ``offer_id``/``device_id`` are what the host attached (L2); ``None`` skips that check (the sandbox owner)."""
    from orch.custody.personkey import BadRecoveryCode, PersonMismatch

    # 1. the request, verified, without the person key
    deadline = time.monotonic() + wait_s
    pending = None
    while pending is None:
        poll = queue.poll(request_id, wait=poll_wait)
        for s in poll.skipped:
            if s.code == "other_open":
                show("a request this command did not create is open; it is left alone")
            elif s.request_id == request_id:
                raise InlineRefused("refused", f"the request was refused ({s.code})", s.code)
        pending = poll.get(request_id)
        if pending is None:
            if time.monotonic() >= deadline:
                raise InlineRefused("timeout", "the cert request did not reach the primary")
            sleep(0.2)

    # 2. bind to the attached offer and device (L2); show what the human decides on
    if offer_id is not None and pending.offer_id != offer_id:
        raise InlineRefused("mismatch", "the request is for another offer than the one attached; nothing was signed")
    if device_id is not None and _bare(pending.device_id) != _bare(device_id):
        raise InlineRefused("mismatch", "the request is for another device than the one paired; nothing was signed")
    if pending.client_hosted:
        show("WARNING: this request says it came from a page your relay operator hosts, not the app: reject unless "
             "you opened that page yourself")  # fmt: skip
    show(f"request from: [{_plain(pending.label)}]")
    wants = ", ".join(pending.scopes_max)
    show(f"  named by the device, not authenticated; device {_short(pending.device_id)}; wants {wants}")

    # 3. one person-key window: challenge, typed cert_code, signature or rejection
    left = recovery_tries
    while True:
        if now_ms() >= pending.expires_ms:
            queue.close(request_id, "expired")
            raise InlineRefused("expired", "the request ended before a recovery code was entered; nothing was signed")
        held = False
        try:
            with pk_factory(labels=ANSWER_LABELS, pk_pub=owner_pk, action=RECOVERY_ACTION) as pk:
                held = True
                return _decide(queue, request_id, pk, pending, ask_cert_code, show, code_tries, now_ms)
        except (BadRecoveryCode, PersonMismatch) as exc:
            if held:
                raise
            left -= 1
            if isinstance(exc, PersonMismatch):
                show("that recovery code is not the one of this workspace's owner; nothing was signed")
            else:
                show("that is not a valid recovery code")
            if left <= 0:
                queue.close(request_id, "cancelled")
                raise InlineRefused("no_key", "no valid recovery code was entered; nothing was signed") from None
            show(f"{left} try/tries left")


def _bare(device: str) -> str:
    return device[2:] if device.startswith("d_") else device


def _decide(
    queue: PrimaryQueue,
    request_id: str,
    pk: Any,
    pending: Pending,
    ask: Callable[[str, Pending, float | None], str | None],
    show: Callable[[str], None],
    tries: int,
    now_ms: Callable[[], int],
) -> PairOutcome:
    try:
        challenged = queue.challenge(request_id, pk)
    except QueueRefused as exc:
        raise InlineRefused("refused", f"the primary refused the request ({exc.code})", exc.code) from None
    expected = challenged.cert_code or ""
    show("the phone now shows a second code")
    for n in range(tries):
        remaining = (pending.expires_ms - now_ms()) / 1000.0
        if remaining <= 0:
            break
        typed = ask(CERT_CODE_PROMPT, challenged, remaining)
        if typed is None or not typed.strip():
            break  # no answer in time, or empty: reject
        code = normalize_code(typed)
        if code is None:
            more = len(typed.split()) >= 2
            show("that did not look like the phone's code; nothing was sent"
                 + ("; never type your recovery code here" if more else ""))  # fmt: skip
            typed = None
            continue
        typed = None
        if hmac.compare_digest(code.encode(), expected.encode()):
            cert = queue.sign(request_id, pk)
            return PairOutcome(request_id, cert=cert, cert_code=expected, label=challenged.label,
                               scopes=challenged.scopes_max, device_id=challenged.device_id)  # fmt: skip
        show(f"that is not the code: {tries - n - 1} try/tries left")
    rejection = queue.reject(request_id, pk)
    return PairOutcome(request_id, cert=None, cert_code=expected, label=challenged.label, scopes=challenged.scopes_max,
                       rejection=rejection, device_id=challenged.device_id)  # fmt: skip
