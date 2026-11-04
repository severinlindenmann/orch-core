"""Domain labels of the hub contract H0 (contracts/hub/protocol.md §3). Every one is new, ends in ``|`` and keeps the union with
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
    "sig_hub_admin_read": "orch/v2/hub-admin-read|",  # F3: hub admin metadata read at orch-publish
    "sig_hub_space": "orch/v2/hub-space|",  # V3-D8 shared spaces (§10.4): a space record at one epoch
    "sig_hub_space_wrap": "orch/v2/hub-space-wrap|",  # V3-D8: SK_e sealed to one member workspace's WXK
    "sig_hub_org_skills": "orch/v2/hub-org-skills|",  # M3 (#1430): company skills offered to one workspace
    "sig_hub_org_secrets": "orch/v2/hub-org-secrets|",  # M3 (#1431): sealed company secrets for one workspace
    "sig_hub_mcp_servers": "orch/v2/hub-mcp-servers|",  # M3 (#1435): remote MCP servers offered to one workspace
    "sig_hub_closing": "orch/v2/hub-closing|",  # #1463: the hub is being removed; offered to one workspace's owner
    "sig_hub_release_narrow": "orch/v2/hub-release-narrow|",  # M4 (#1357): a hub admin revokes a link / stops an app
    "sig_hub_allow": "orch/v2/hub-allow|",  # P02 (#1290): the hub admits one of its own workspaces at orch-publish
    "sig_hub_publish_where": "orch/v2/hub-publish-where|",  # #1838: where the hub's workspaces publish and share
    "sig_hub_app_pass": "orch/v2/hub-app-pass|",  # M4 (#1357): single-use pass into a company or group app
    "sig_hub_space_proposal": "orch/v2/hub-space-proposal|",  # p4 (#1020): a proposed member transition, to an invitee
    "sig_hub_relay_request": "orch/v2/hub-relay-req|",
    "sig_hub_api_origin": "orch/v2/hub-api-origin|",  # p4 (B, §6.4, L2B-1): the hub names its own public API origin  # p4 (E1): a hub -> relay request, bytes as admin read
    # signatures by a channel key
    "sig_hub_channel_proof": "orch/v2/hub-channel-proof|",
    "sig_hub_channel_register": "orch/v2/hub-channel-reg|",  # p4: proof of possession at channel registration
    "sig_hub_channel_auth": "orch/v2/hub-channel-auth|",  # p4 (B, §6.4): binds a pairing session to the channel
    "sig_hub_channel_push": "orch/v2/hub-channel-push|",  # p4 (B, §6.4): names the channel's push key
    "sig_hub_channel_relay_req": "orch/v2/hub-channel-relay-req|",  # p4 (B, §10.8.4): a channel -> relay request
    # signatures by WSK (workspace host)
    "sig_hub_binding": "orch/v2/sig/hub-binding|",
    "sig_hub_unbound": "orch/v2/sig/hub-unbound|",
    "sig_hub_link": "orch/v2/sig/hub-link|",
    "sig_hub_link_auth": "orch/v2/sig/hub-link-auth|",
    "sig_space_item": "orch/v2/sig/space-item|",  # V3-D8: a member workspace's item in a shared space
    "sig_space_accept": "orch/v2/sig/space-accept|",  # L2P-2: a selective workspace's owner accepts a member set
    "sig_publish_hub": "orch/v2/publish-hub|",  # a publish request on the hub path (L2-2); v2 keeps orch/v2/publish|
    # signature by the workspace owner's device key (V3-D4)
    "sig_hub_publish_approval": "orch/v2/sig/hub-publish-approval|",
    # signature by the person key PK (derived from the recovery code; #1198 C8, §9.6): the owner proof of a hub op
    "sig_hub_owner_proof": "orch/v2/sig/hub-owner-proof|",
    # hashes
    "h_hub_id": "orch/v2/id/hub|",
    "h_channel_id": "orch/v2/id/channel|",
    "h_hub_event_digest": "orch/v2/hub-event-digest|",
    "h_hub_publish_request": "orch/v2/hub-publish-request|",
    "h_publish_request_direct": "orch/v2/publish-request|",  # M4: what an approval names on the direct (12.1) path
    "h_app_meta": "orch/v2/app-meta|",  # M4: the stored meta of one app version (commit binds it)
    "h_app_reach": "orch/v2/app-reach|",  # M4: the approved reach list of one app version
    "h_hub_session_ref": "orch/v2/hub-session-ref|",  # p4 (B, §6.4): names a pairing session inside channel_auth
    "h_hub_audit_chain": "orch/v2/hub-audit-chain|",  # D4: hash of one signed audit record (the chain link)
    "h_hub_audit_scope": "orch/v2/hub-audit-scope|",  # L2P-4: blinded workspace tag of an audit record
    "h_hub_audit_event": "orch/v2/hub-audit-event|",  # D4: hash of an audit event, signed into its record
    "h_hub_market_policy": "orch/v2/hub-market-policy|",  # D14: hash of the hub marketplace policy (audit + console)
    "h_space_members": "orch/v2/hub-space-members|",  # L2P-1: an item signs the member set of its epoch
    "h_sk_commit": "orch/v2/hub-sk-commit|",  # V3-D8: commitment to SK_e inside the signed space record
    # AAD of a sealed space item
    "aad_space": "orch/v2/space-aad|",
    # p4 (§10.2b, C6/D9): a selective workspace's shared item, its AAD and its hash
    "sig_share_item": "orch/v2/sig/share-item|",
    "aad_hub_share": "orch/v2/hub-share-aad|",
    "h_share_item": "orch/v2/hub-share-item|",
    # AAD of a sealed workspace-view page (p4, log_sync)
    "aad_hub_view": "orch/v2/hub-view-aad|",
}

#: HKDF labels and seal purposes of V3-D8 (not signed/AAD bytes, so outside the prefix-free table; protocol-v2 §3 rules:
#: ``info`` = label followed by ``|`` fields).
KDF_SPACE_ITEM = b"orch/v2/space-item"  # HKDF(SK_e, "", label|space|epoch) -> the space's item key
KDF_SPACE_ITEM_MSG = b"orch/v2/space-item-msg"  # HKDF(item key, salt16, label) -> one item's AEAD key
KDF_HUB_VIEW = b"orch/v2/hub-view"  # p4: HKDF(WK_e, "", label|ws|epoch) -> the view key of one epoch
KDF_HUB_VIEW_MSG = b"orch/v2/hub-view-msg"  # p4: SALTED-AEAD msg label of one view page
KDF_HUB_SHARE_MSG = b"orch/v2/hub-share-msg"  # p4: SALTED-AEAD msg label of one shared item under its item key
PURPOSE_HUB_SHARE = "hub-share"  # p4: seal_to purpose: a shared item's key to the hub's HXK (amends v2 §5.3)
PURPOSE_SPACE_SK = (
    "space-sk"  # seal_to purpose: SK_e to a member workspace's WXK (an addition to v2 §5.3's closed list)
)

#: M2-8a: the domain of the digest an owner's channel key signs to approve an access change (``changes/proof.py``). A
#: hub-local console label like the HKDF ones above: it names no contract object, so it stays out of ``HUB_LABELS`` (the H0
#: vector file and protocol.md list those); ``tests/test_access_changes.py`` checks it is prefix-free with every label.
ACCESS_CHANGE_LABEL = "orch/v2/hub-access-change|"
ACCESS_CHANGE_KIND = "orch.access_change.v1"

#: #1463: the domain of the digest an admin's channel key signs to remove the hub (a console label like the one above: the
#: hub reads it, no workspace does; protocol.md §9.7 "The removal proof"). Prefix-free with every label.
HUB_REMOVE_LABEL = "orch/v2/hub-remove|"
HUB_REMOVE_KIND = "orch.hub_remove.v1"

#: UK signs only under this family: ``"orch/v2/hub-custodial|" + <event type> + "|"`` (spec §5).
CUSTODIAL_PREFIX = "orch/v2/hub-custodial|"
EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}")

#: Existing v2 labels this contract reuses as they are.
V2_REUSED: dict[str, str] = {
    "sig_publish": "orch/v2/publish|",
    "aad_seal": "orch/v2/seal-aad|",
    "sig_device_cert": "orch/v2/sig/device-cert|",
    "sig_ws_event": "orch/v2/sig/ws-event|",
    "sig_ws_delegation": "orch/v2/sig/ws-delegation|",
    "sig_card_wsk": "orch/v2/sig/card-wsk|",
    "h_person_id": "orch/v2/id/person|",
    "h_device_id": "orch/v2/id/device|",
    "sig_host_event": "orch/v2/sig/host-event|",  # ticket-format §5.5: host_sig of every log line (log_sync)
    "h_event": "orch/v2/event|",  # ticket-format §5.5: the event head (log_sync chain)
}

L: dict[str, bytes] = {k: v.encode("ascii") for k, v in {**HUB_LABELS, **V2_REUSED}.items()}


def custodial_label(event_type: object) -> bytes:
    """The UK signing domain for one event type; refuses anything outside :data:`EVENT_TYPE` (so no ``|``)."""
    if type(event_type) is not str or not EVENT_TYPE.fullmatch(event_type):
        raise ValueError("event type outside the custodial label grammar")
    return (CUSTODIAL_PREFIX + event_type + "|").encode("ascii")
