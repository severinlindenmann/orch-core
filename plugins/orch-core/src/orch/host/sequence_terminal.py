"""``orch sign CODE`` for the first request of a sequence (batch 11, finding 9): the terminal path.

One review (the whole intent: the ticket and every written section), one confirmation word, ONE passphrase unlock for
the batch (the key is open only for the sequence; the scalar is zeroised when it ends), then the engine of
:mod:`orch.host.sequence` signs every step. A custody that signs without a factor (one-click) signs step by step.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import Any

from orch import canon

from . import sequence, signer

__all__ = ["request_backend", "sign_sequence", "unlocked_backend"]


class _Unlocked:
    """What an open batch signs through: ``sign_person_event`` and :func:`signer.sign` ask only for these."""

    name = "passphrase"
    person_capable = True

    def __init__(self, auth: str, raw: Callable[[bytes], bytes]) -> None:
        self.auth, self._raw = auth, raw

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        return self._raw(payload)


@contextmanager
def request_backend(
    backend: Any, key_id: str, prepared: Mapping[str, Any], events: list[Mapping[str, Any]], workspace_id: str
) -> Iterator[Any]:
    """The backend to sign the events of ONE reviewed request (an Extend: the new grant and the take-back of the old):
    one passphrase for them where the custody can do it. The batch may sign the event label of the request's log and no
    other; the person has seen every event in the one review."""
    import hashlib

    label = "sig_ws_event" if prepared["log"] == "workspace" else "sig_ticket_event"
    with unlocked_backend(
        backend,
        key_id,
        digest=hashlib.sha256(canon.dumps(events)).hexdigest(),
        fields=(("signatures", str(len(events))), ("workspace", workspace_id)),
        action=f"{prepared['op']}: {len(events)} signatures, one review",
        labels=(canon.LABELS[label].encode("ascii"),),
    ) as be:
        yield be


def labels_for(intent: Mapping[str, Any]) -> tuple[bytes, ...]:
    """The domain labels the reviewed intent's steps need, and no more: a ticket event's label for every step on the
    ticket, the workspace event's label only when a step is on the workspace log: the grant (``grant.issued``) or a step
    that names ``log: "workspace"`` (the settings and add-on events of ``addon_enable``)."""
    names = [
        "sig_ws_event" if s["kind"] == "grant" or s.get("log") == "workspace" else "sig_ticket_event"
        for s in sequence.steps(intent)
    ]
    return tuple(canon.LABELS[n].encode("ascii") for n in dict.fromkeys(names))


@contextmanager
def unlocked_backend(
    backend: Any,
    key_id: str,
    *,
    digest: str,
    fields: tuple[tuple[str, str], ...],
    action: str,
    labels: tuple[bytes, ...] | None = None,
) -> Iterator[Any]:
    """The backend to sign the whole sequence with: one passphrase for the batch where the custody can do it, else the
    backend itself (it signs without a factor, or asks per signature). ``labels`` are the domain labels the batch may
    sign (:func:`labels_for`); without them, one ticket's events only. The custody refuses any other label."""
    opener = getattr(backend, "unlocked", None)
    if opener is None:
        yield backend
        return
    if labels is None:
        labels = (canon.LABELS["sig_ticket_event"].encode("ascii"),)
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
    deliver: Callable[[str, str], list[str]] | None = None,
    now: Callable[[], float] = time.time,
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
        if intent.get("images"):  # the signer holds the pasted files; a terminal has none to upload (R3 part B)
            signer._refuse("pasted screenshots are signed with a click in the dashboard, not in the terminal")
        driver = sequence.Driver(
            _port(call, backend, key_id, workspace_id, person, device, auth, workspace, new_idem, {}),
            sessions,
            workspace_id=workspace_id,
            person=person,
            intent=intent,
            now=now,
        )
        resumed = driver.resumable()
        review = signer.plain(prepared, workspace, prefix=prefix, members=signer.members_of(call))
        if resumed is not None:
            n = sum(1 for s in resumed["steps"] if s["state"] == "landed")
            if resumed["ticket"] is None:  # a sequence on the workspace log (turning on an add-on): no ticket
                note = f"RESUMING: {n} of {driver.total} signatures of this exact review are already signed.\n\n"
            else:
                note = (
                    f"RESUMING: {n} of {driver.total} signatures of this exact review are already in the ticket "
                    f"{signer._clean(resumed['ticket']['key'])} (key per host; signed id "
                    f"…{signer._clean(str(resumed['ticket']['uid'])[-8:])}); no second ticket is made.\n\n"
                )
            review = _prefixed(review, note)
        if not confirm(review, signer.confirm_word(prepared, workspace)):
            signer._refuse("not confirmed")
    except BaseException as exc:
        signer.cancel_request(call, request_id, exc)
        raise
    box: dict[str, Any] = {}
    fields = (
        ("signatures", str(driver.total)),
        ("add-on" if intent["kind"] == sequence.ADDON else "ticket title", sequence.label_of(intent)),
        ("workspace", workspace_id),
    )
    with ExitStack() as stack:
        try:  # the one passphrase prompt of the batch: an interrupt or refusal here is still before any signature
            be = stack.enter_context(
                unlocked_backend(
                    backend,
                    key_id,
                    digest=driver.digest,
                    fields=fields,
                    action=f"orch: {driver.total} signatures, one review",
                    labels=labels_for(intent),
                )
            )
        except BaseException as exc:
            signer.cancel_request(call, request_id, exc)
            raise
        box["backend"] = be
        port = _port(call, be, key_id, workspace_id, person, device, auth, workspace, new_idem, box)
        driver.port = port
        out = driver.run(prepared)
    last = out["results"][-1] if out["results"] else {}
    result: dict[str, Any] = {"key": out["key"], "seq": last.get("seq"), "steps": out["landed"]}
    secret = box.pop("secret", None)
    if secret is not None and deliver is not None:  # the grant is the last step: its secret only after the commit
        result["lines"] = deliver(secret, out["requests"][-1])
    return result


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
        signed = signer.sign(prepared, backend=box.get("backend", backend), key_id=key_id, workspace_id=workspace_id)
        if signed.secret is not None:  # a sequence's grant: kept in this frame until the commit, never recorded
            box["secret"] = signed.secret
        return signed.events

    def submit(rid: str, events: list[dict[str, Any]], idem: str) -> dict[str, Any]:
        res = call("human.submit", dict(request_id=rid, events=events), idem=idem)
        return res if isinstance(res, dict) else {}

    def head(key: str) -> dict[str, Any]:
        data = call("ticket.get", {"ref": key})["data"]
        return {**data["doc"]["head"], "tail": (data.get("log") or [None])[-1]}

    def cancel(rid: str) -> None:
        call("human.cancel", {"request_id": rid})

    return sequence.Port(
        prepare=prepare, check=check, sign=sign, submit=submit, new_idem=new_idem, head=head, cancel=cancel
    )
