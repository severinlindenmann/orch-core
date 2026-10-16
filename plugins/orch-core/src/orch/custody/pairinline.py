"""The CLI's inline primary driver (orch-v2-host 11A.15.23, B1b): what ``orch host relay pair --primary inline`` runs.

It lives here, not in ``orch.host``: the person path's relay client (``orch.relay_owner``) is never imported by the
host daemon or the bridge client (N4, ``tests/relay_owner/test_boundary.py``). ``orch.host.paircli`` (the CLI) reaches
it through this module and passes in its terminal rules (``show``, the link check, the error type).

One call = one request: the primary passphrase (one prompt), the install's relay (``relay.json``), the host's
stored card verified under the workspace owner's key, then :func:`orch.relay_owner.inline.serve_request`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["driver"]


def driver(
    state: Path,
    entry: Mapping[str, Any],
    *,
    show: Callable[[str], None],
    ask_timed: Callable[[str, float | None], str | None],
    link_check: Callable[[], None],
    fail: Callable[..., Exception],
) -> Callable[[str, str, Mapping[str, Any]], Any]:
    """``primary(offer_id, request_id, offer)`` for ``paircli.attach``. ``fail(code, message, hint=None)`` builds the
    caller's error type."""

    def run(offer_id: str, rid: str, offer: Mapping[str, Any]) -> Any:
        from orch.custody import primary, primary_register
        from orch.custody.cli import _Fail, _pk_factory, owner_pk_from_workspace
        from orch.relay_owner import OwnerClient, PrimaryJournal, PrimaryQueue, inline, journal_dir

        wid = entry["workspace_id"]
        rel = primary_register.read_relay(state)
        if rel is None:
            raise fail(
                "primary.not_registered",
                "this install has no registered primary",
                hint="orch keys primary init --workspace W --relay ORIGIN --invite-file F",
            )
        link_check()
        device_id = str(offer.get("device_id") or "")
        if not device_id:  # fail closed: without the consumed device the request cannot be bound to it (L2)
            raise inline.InlineRefused("mismatch", "the host named no device for this offer; nothing was signed")
        try:
            owner_pk = owner_pk_from_workspace(state, entry)
        except _Fail as exc:
            raise fail(exc.code, exc.message) from None
        card = inline.stored_card(state, wid, owner_pk)
        jdir = journal_dir(state)
        jdir.mkdir(parents=True, exist_ok=True, mode=0o700)

        def ask_code(prompt: str, _pending: Any, deadline_s: float | None) -> str | None:
            return ask_timed(prompt, deadline_s)

        show("approve the phone: your primary passphrase is asked first")
        with primary.unlocked(state, action="pair a device") as session:
            client = OwnerClient(rel["origin"], session)
            try:
                queue = PrimaryQueue(
                    client, PrimaryJournal(jdir), pk_pub=owner_pk,
                    cards=lambda ws: card if ws == card.workspace_id else None,
                    unseal=primary.cert_request_unseal(session),
                )  # fmt: skip
                queue.finish_uploads()  # a decision a crash left unsent goes out first
                return inline.serve_request(
                    queue, rid, pk_factory=_pk_factory, ask_cert_code=ask_code, show=show, owner_pk=owner_pk,
                    offer_id=offer_id, device_id=device_id, attempt=offer.get("attempt") or None,
                )  # fmt: skip
            finally:
                client.close()

    return run
