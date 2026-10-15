"""The owner relay client (orch-v2-host 11A.15.10 item 11, "As built in H12c-f"): the person path's relay client.

Not part of ``orch.relay`` (the host bridge client, which holds no device or person key): this package logs in as
the person's primary device, creates the relay account, registers device certificates and works the primary's
cert-request queue. See :mod:`orch.relay_owner.client` (routes and transport) and :mod:`orch.relay_owner.queue` and
:mod:`orch.relay_owner.journal` (the primary steps of protocol §8.4 and their durable journal).
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

__all__ = [
    "MAX_LISTED_REQUESTS",
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
