"""Domain labels of the hub contract H0 (docs/protocol.md §3). Every one is new, ends in ``|`` and keeps the union with
the orch v2 labels (orch-relay protocol-v2 §3, orch-core ``orch.crypto.labels`` and ``orch.canon.LABELS``)
prefix-free; ``tests/test_labels.py`` checks that against a frozen copy of the v2 table."""

from __future__ import annotations

import re

HUB_LABELS: dict[str, str] = {
    # signatures by HK_root (offline root)
    "sig_hub_op_cert": "orch/v2/hub-op-cert|",
    "sig_hub_op_revoked": "orch/v2/hub-op-revoked|",
    # signatures by HK_op (spec §5 names hub-card, hub-uk-cert, hub-snapshot, hub-route, hub-publish, hub-audit)
    "sig_hub_card": "orch/v2/hub-card|",
    "sig_hub_uk_cert": "orch/v2/hub-uk-cert|",
    "sig_hub_channel_cert": "orch/v2/hub-channel-cert|",
    "sig_hub_snapshot": "orch/v2/hub-snapshot|",
    "sig_hub_route": "orch/v2/hub-route|",
    "sig_hub_publish": "orch/v2/hub-publish|",
    "sig_hub_audit": "orch/v2/hub-audit|",  # reserved for D4 (audit chain); no object in H0
    # signatures by a channel key
    "sig_hub_channel_proof": "orch/v2/hub-channel-proof|",
    # signatures by WSK (workspace host)
    "sig_hub_binding": "orch/v2/sig/hub-binding|",
    "sig_hub_unbound": "orch/v2/sig/hub-unbound|",
    "sig_hub_link": "orch/v2/sig/hub-link|",
    "sig_hub_link_auth": "orch/v2/sig/hub-link-auth|",
    "sig_publish_hub": "orch/v2/publish-hub|",  # a publish request on the hub path (L2-2); v2 keeps orch/v2/publish|
    # signature by the workspace owner's device key (V3-D4)
    "sig_hub_publish_approval": "orch/v2/sig/hub-publish-approval|",
    # hashes
    "h_hub_id": "orch/v2/id/hub|",
    "h_channel_id": "orch/v2/id/channel|",
    "h_hub_event_digest": "orch/v2/hub-event-digest|",
    "h_hub_publish_request": "orch/v2/hub-publish-request|",
}

#: UK signs only under this family: ``"orch/v2/hub-custodial|" + <event type> + "|"`` (spec §5).
CUSTODIAL_PREFIX = "orch/v2/hub-custodial|"
EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}")

#: Existing v2 labels this contract reuses as they are.
V2_REUSED: dict[str, str] = {
    "sig_publish": "orch/v2/publish|",
    "sig_device_cert": "orch/v2/sig/device-cert|",
    "sig_ws_event": "orch/v2/sig/ws-event|",
    "h_person_id": "orch/v2/id/person|",
    "h_device_id": "orch/v2/id/device|",
}

L: dict[str, bytes] = {k: v.encode("ascii") for k, v in {**HUB_LABELS, **V2_REUSED}.items()}


def custodial_label(event_type: object) -> bytes:
    """The UK signing domain for one event type; refuses anything outside :data:`EVENT_TYPE` (so no ``|``)."""
    if type(event_type) is not str or not EVENT_TYPE.fullmatch(event_type):
        raise ValueError("event type outside the custodial label grammar")
    return (CUSTODIAL_PREFIX + event_type + "|").encode("ascii")
