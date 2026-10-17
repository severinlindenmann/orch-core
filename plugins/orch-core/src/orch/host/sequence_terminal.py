"""``orch sign CODE`` for the first request of a sequence (batch 11, finding 9): the terminal path.

One review (the whole intent: the ticket and every written section), one confirmation word, ONE passphrase unlock for
the batch (the key is open only for the sequence; the scalar is zeroised when it ends), then the engine of
:mod:`orch.host.sequence` signs every step. A custody that signs without a factor (one-click) signs step by step.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from orch import canon

from . import sequence, signer

__all__ = ["sign_sequence", "unlocked_backend"]


class _Unlocked:
    """What an open batch signs through: ``sign_person_event`` and :func:`signer.sign` ask only for these."""

    name = "passphrase"
    person_capable = True

    def __init__(self, auth: str, raw: Callable[[bytes], bytes]) -> None:
        self.auth, self._raw = auth, raw

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        return self._raw(payload)


@contextmanager
def unlocked_backend(
    backend: Any, key_id: str, *, digest: str, fields: tuple[tuple[str, str], ...], action: str
) -> Iterator[Any]:
    """The backend to sign the whole sequence with: one passphrase for the batch where the custody can do it, else the
    backend itself (it signs without a factor, or asks per signature)."""
    opener = getattr(backend, "unlocked", None)
    if opener is None:
        yield backend
        return
    labels = (canon.LABELS["sig_ticket_event"].encode("ascii"),)  # a sequence signs one ticket's events only
    with opener(key_id, action=action, digest=digest.split(":")[-1][:32], fields=fields, labels=labels) as raw:
        yield _Unlocked(str(backend.auth), raw)


def sign_sequence(
    call: Callable[..., Any],
    prepared: Mapping[str, Any],
    request_id: str,
    *,
    backend: Any,
    key_id: str,
    workspace_id: str,
    person: str,
    device: str,
    workspace: Callable[[str], tuple[str, str] | None],
    confirm: Callable[[str, str], bool],
    sessions: sequence.Sessions,
    new_idem: Callable[[], str],
    prefix: str = "",
    before: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Check, review ONCE, confirm ONCE, then sign every step. Raises :class:`signer.SignerRefused` before anything is
    signed (the request is cancelled), :class:`sequence.Aborted` when the sequence ends after some signatures (what
    landed stays a draft). Returns ``{"key", "seq", "steps"}``."""
    auth = backend.auth
    try:
        if before is not None:
            before()
        signer.check(prepared, workspace_id=workspace_id, person=person, device=device, auth=auth)
        intent = sequence.validate_intent(prepared["material"]["sequence"]["intent"])
        driver = sequence.Driver(
            _port(call, backend, key_id, workspace_id, person, device, auth, workspace, new_idem, {}),
            sessions,
            workspace_id=workspace_id,
            person=person,
            intent=intent,
        )
        resumed = driver.resumable()
        review = signer.plain(prepared, workspace, prefix=prefix)
        if resumed is not None:
            n = sum(1 for s in resumed["steps"] if s["state"] == "landed")
            note = (
                f"RESUMING: {n} of {driver.total} signatures of this exact review are already in the ticket "
                f"{signer._clean(resumed['ticket']['key'])}; no second ticket is made.\n\n"
            )
            review = _prefixed(review, note)
        if not confirm(review, signer.confirm_word(prepared, workspace)):
            signer._refuse("not confirmed")
    except BaseException:
        try:
            call("human.cancel", {"request_id": request_id})
        except Exception:  # noqa: BLE001 - it expires in 10 minutes anyway
            pass
        raise
    box: dict[str, Any] = {}
    fields = (("signatures", str(driver.total)), ("ticket title", intent["title"]), ("workspace", workspace_id))
    with unlocked_backend(
        backend, key_id, digest=driver.digest, fields=fields, action=f"orch: {driver.total} signatures, one review"
    ) as be:
        box["backend"] = be
        port = _port(call, be, key_id, workspace_id, person, device, auth, workspace, new_idem, box)
        driver.port = port
        out = driver.run(prepared)
    last = out["results"][-1] if out["results"] else {}
    return {"key": out["key"], "seq": last.get("seq"), "steps": out["landed"]}


def _prefixed(review: signer.Review, note: str) -> signer.Review:
    out = signer.Review(note + str(review))
    out.details = note + review.details if review.details else ""
    out.details_first = review.details_first
    return out


def _port(
    call: Callable[..., Any],
    backend: Any,
    key_id: str,
    workspace_id: str,
    person: str,
    device: str,
    auth: str,
    workspace: Callable[[str], tuple[str, str] | None],
    new_idem: Callable[[], str],
    box: dict[str, Any],
) -> sequence.Port:
    def prepare(op: str, args: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        return call(
            "human.prepare",
            {"op": op, "args": args, "signer": {"device": device, "auth": auth}, "sequence": meta},
        )["data"]

    def check(prepared: dict[str, Any]) -> None:
        signer.check(prepared, workspace_id=workspace_id, person=person, device=device, auth=auth)

    def sign(prepared: dict[str, Any]) -> list[dict[str, Any]]:
        return signer.sign(
            prepared, backend=box.get("backend", backend), key_id=key_id, workspace_id=workspace_id
        ).events

    def submit(rid: str, events: list[dict[str, Any]], idem: str) -> dict[str, Any]:
        res = call("human.submit", {"request_id": rid, "events": events}, idem=idem)
        return res if isinstance(res, dict) else {}

    def head(key: str) -> dict[str, Any]:
        doc = call("ticket.get", {"ref": key})["data"]["doc"]
        return dict(doc["head"])

    def cancel(rid: str) -> None:
        call("human.cancel", {"request_id": rid})

    return sequence.Port(
        prepare=prepare, check=check, sign=sign, submit=submit, new_idem=new_idem, head=head, cancel=cancel
    )
