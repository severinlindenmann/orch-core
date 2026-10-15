"""The owner relay client (orch-v2-host 11A.15.10 item 11, "As built in H12c-f"): the person path's relay client.

Not part of ``orch.relay`` (the host bridge client, which holds no device or person key): this package logs in as
the person's primary device, creates the relay account, registers device certificates and works the primary's
cert-request queue. See :mod:`orch.relay_owner.client` (routes and transport) and :mod:`orch.relay_owner.queue` and
:mod:`orch.relay_owner.journal` (the primary steps of protocol §8.4 and their durable journal).

**API for the inline primary and the test owner client (orch-29, N6).** One command::

    with primary.unlocked(state, action="pair a device") as session:          # one passphrase prompt
        client = OwnerClient(origin, session)                                 # RelayConn to origin; logs in lazily
        q = PrimaryQueue(client, PrimaryJournal(journal_dir(state)), pk_pub=owner_pk,
                         cards=lambda ws: verified_card_or_None, unseal=primary.cert_request_unseal(session))
        poll = q.poll(wait=None)            # Poll(pending, skipped); warn on every pending you did not create
        p = poll.get(request_id_r6_reported)
        with RecoveryCodeSource(labels=ANSWER_LABELS, pk_pub=owner_pk, action=...) as pk:   # Q1 = B
            p = q.challenge(p, pk)          # show p.cert_code, p.label ("named by the device"), p.scopes_max
            cert = q.sign(p, pk)            # after the human typed cert_code; or q.reject(p, pk)
        # hand ``cert`` to R6 (relay.pair_cert)

* account: ``client.create_account(invite, owner_pk, primary_certificate(pk, session, session.dk_kx_pub,
  label=..., created_ms=...))`` (``pk`` with ``labels={"sig_device_cert"}``); a second relay:
  ``client.register_device(cert, owner_pk)``; ``client.get_device(id, owner_pk)``.
* ``q.finish_uploads()`` resends decided-but-not-uploaded bytes after a crash; ``q.close(id, "cancelled")`` ends a
  request of a cancelled offer.
* Tests (``tests/relay/owner.py``, the T2 kit) pass ``transport=RelayConn(loopback)`` with the signed ``origin``
  kept, a vector ``DeviceSigner`` (``device_id``, ``dk_sig_pub``, ``sign``) and any object with ``pk_pub``,
  ``signer(label)``, ``derive_label_key()`` as the person key (``tests/relay_owner/kit.py``: ``VecSigner``,
  ``VecPK``). ``max_scopes`` widens the v1.7 phone ceiling only when the caller says so (the vector inner asks for
  every scope).
* Errors: :class:`OwnerTransportError` / :class:`OwnerRefused` (``transient`` says retry) /
  :class:`OwnerMisbehaving` (alarm ``relay_misbehaving``) / :class:`OwnerRequestError`; :class:`QueueRefused`;
  :class:`JournalError` (``journal.busy``, ``journal.full``, ``journal.corrupt``) and :class:`RequestConflict`.
"""

from .client import (
    MAX_LISTED_REQUESTS,
    ROUTES,
    DeviceSigner,
    OwnerClient,
    OwnerError,
    OwnerMisbehaving,
    OwnerRefused,
    OwnerRequestError,
    OwnerTransportError,
    Route,
    primary_certificate,
)
from .journal import Entry, JournalError, PrimaryJournal, RequestConflict, journal_dir
from .queue import ANSWER_LABELS, PHONE_SCOPES, Pending, Poll, PrimaryQueue, QueueRefused, Skipped

__all__ = [
    "ANSWER_LABELS",
    "MAX_LISTED_REQUESTS",
    "PHONE_SCOPES",
    "Entry",
    "JournalError",
    "Pending",
    "Poll",
    "PrimaryJournal",
    "PrimaryQueue",
    "QueueRefused",
    "RequestConflict",
    "Skipped",
    "journal_dir",
    "ROUTES",
    "DeviceSigner",
    "OwnerClient",
    "OwnerError",
    "OwnerMisbehaving",
    "OwnerRefused",
    "OwnerRequestError",
    "OwnerTransportError",
    "Route",
    "primary_certificate",
]
