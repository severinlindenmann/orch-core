"""Protocol domain-separation labels (orch-relay protocol-v2 §3). One place; ``tests/crypto/test_vectors.py`` checks
the table against ``vectors_v2.json`` and that the signature, AAD and hash labels are prefix-free.

The event-signing and checkpoint labels of ticket-format §5.3 live in :data:`orch.canon.LABELS`; use those for events.
The vector table lists them too (relay sync), so the registry test compares this table plus the canon ones.
"""

from __future__ import annotations

__all__ = ["L", "LABELS", "PLANNED_LABELS", "SIGNATURE_LABEL_KEYS", "signature_labels"]

LABELS: dict[str, str] = {
    "kdf_bridge": "orch/v2/bridge",
    "kdf_bridge_msg": "orch/v2/bridge-msg",
    "kdf_push": "orch/v2/push",
    "kdf_push_msg": "orch/v2/push-msg",
    "kdf_wrap_wk": "orch/v2/wrap-wk",
    "kdf_wrap_sk": "orch/v2/wrap-sk",
    "kdf_wrap_msg": "orch/v2/wrap-msg",
    "kdf_pair": "orch/v2/pair",
    "kdf_seal": "orch/v2/seal",
    "kdf_card": "orch/v2/card",
    "kdf_drop_content": "orch/v2/drop-content",
    "kdf_drop_meta": "orch/v2/drop-meta",
    "kdf_label": "orch/v2/label",
    "kdf_label_msg": "orch/v2/label-msg",
    "aad_seal": "orch/v2/seal-aad|",
    "aad_push": "orch/v2/push-aad|",
    "aad_wrap": "orch/v2/wrap-aad|",
    "aad_card": "orch/v2/card-aad|",
    "aad_label": "orch/v2/label-aad|",
    "aad_drop_meta": "orch/v2/drop-meta-aad|",
    "sig_device_cert": "orch/v2/sig/device-cert|",
    "sig_revocation": "orch/v2/sig/revocation|",
    "sig_card_wsk": "orch/v2/sig/card-wsk|",
    "sig_ws_delegation": "orch/v2/sig/ws-delegation|",
    "sig_cert_challenge": "orch/v2/sig/cert-challenge|",
    "sig_push": "orch/v2/sig/push|",
    "sig_decision": "orch/v2/sig/decision|",
    "sig_member_list": "orch/v2/sig/member-list|",
    "sig_wk_grant": "orch/v2/sig/wk-grant|",
    "sig_sk_grant": "orch/v2/sig/sk-grant|",
    "sig_bridge": "orch/v2/sig/bridge|",
    "sig_cert_request": "orch/v2/sig/cert-request|",
    "sig_genesis_pin": "orch/v2/sig/genesis-pin|",
    "sig_cert_rejection": "orch/v2/sig/cert-rejection|",
    "sig_enroll_request": "orch/v2/sig/enroll-request|",
    "sig_drop_object": "orch/v2/sig/drop-object|",
    "sig_drop_claim": "orch/v2/sig/drop-claim|",
    "sig_ws_envelope": "orch/v2/ws-envelope|",
    "sig_ws_cosign": "orch/v2/sig/ws-cosign|",
    "sig_webauthn_bind": "orch/v2/sig/webauthn-bind|",
    "sig_relay_auth": "orch/v2/sig/relay-auth|",
    "sig_publish": "orch/v2/publish|",
    "h_device_id": "orch/v2/id/device|",
    "h_person_id": "orch/v2/id/person|",
    "h_pin_person": "orch/v2/pin/person|",
    "h_pin_workspace": "orch/v2/pin/workspace|",
    "h_sas": "orch/v2/sas|",
    "h_sas_cert": "orch/v2/sas-cert|",
    "h_dk_hash": "orch/v2/dk-hash|",
    "h_wk_commit": "orch/v2/wk-commit|",
    "h_event": "orch/v2/event|",
    "h_gate": "orch/v2/gate|",
    "h_grant_secret": "orch/v2/grant-secret|",
    "h_people": "orch/v2/people|",
    "h_policy": "orch/v2/policy|",
    "h_question_id": "orch/v2/question-id|",
    "h_section": "orch/v2/section|",
    "h_value": "orch/v2/value|",
    "h_dek_commit": "orch/v2/dek-commit|",
    "h_card_key_commit": "orch/v2/card-key-commit|",
    "h_drop_parent": "orch/v2/drop-parent|",
    "h_ws_envelope": "orch/v2/ws-envelope-hash|",
    "h_question": "orch/v2/question|",
    "h_assert": "orch/v2/assert|",
    "h_webauthn_reg": "orch/v2/webauthn-reg|",
    "mac_enroll": "orch/v2/enroll|",
}

PLANNED_LABELS: dict[str, str] = {
    # H13-0 (orch-v2-host 11A.16.2, 11A.16.6, 11A.16.11): host-local labels, no relay or phone format. Not in the relay
    # corpus, so kept apart from LABELS (which mirrors it); tests/crypto/test_h13_labels.py keeps them prefix-free.
    "kdf_host_drop_recovery": "orch/v2/host-drop-recovery|",  # the host-only Drop recovery envelope (D7, Q4)
    "sig_drop_handoff": "orch/v2/sig/drop-handoff|",  # the terminal hand-off (O1); signable from H13b-4 only
    # V3 C2a (orch-hub contract H0, section 3): what the WSK signs for a hub. Hub-side labels (op cert, card, snapshot,
    # route ...) are signed by the hub's keys and never by core, so they are not here.
    "sig_hub_binding": "orch/v2/sig/hub-binding|",
    "sig_hub_unbound": "orch/v2/sig/hub-unbound|",
    "sig_hub_link": "orch/v2/sig/hub-link|",
    "sig_hub_link_auth": "orch/v2/sig/hub-link-auth|",
    # V3 C7 (hub contract 11.1): the WSK signature of a publish request that goes through the hub (its own label, so it
    # never verifies as a direct v2 12.1 request).
    "sig_publish_hub": "orch/v2/publish-hub|",
    # V3 C13 (hub contract 10.4, shared spaces): what the WSK signs (an item, an accept) and the hashes / AAD.
    # The HK_op labels of hub.space and hub.space_wrap are the hub's and are only verified (hubclient/spaces.py).
    "sig_space_item": "orch/v2/sig/space-item|",
    "sig_space_accept": "orch/v2/sig/space-accept|",
    "h_space_members": "orch/v2/hub-space-members|",
    "h_sk_commit": "orch/v2/hub-sk-commit|",
    "aad_space": "orch/v2/space-aad|",
    # V3 C6 (hub contract 10.2b, selective share_put): what the WSK signs (a share.item), its content AAD and its hash.
    "sig_share_item": "orch/v2/sig/share-item|",
    # #1097 (key exposure): the person key's fresh, workspace-bound statement that a new device replaces an old one.
    "sig_device_rotate": "orch/v2/sig/device-rotate|",
    # #1198 C9 (hub contract 9.6): the person key's owner proof of a hub join, mode switch or leave.
    "sig_hub_owner_proof": "orch/v2/sig/hub-owner-proof|",
    # R2 (#1590, hub contract 10.2c "The owner proof of a remote step"): the same proof object under its OWN label, so a
    # hub proof never stands in for a remote one or the reverse. Core-only: the hub never sees these proofs.
    "sig_remote_owner_proof": "orch/v2/sig/remote-owner-proof|",
    # R2 (#1590, hub contract 10.2c "The device list"): the WSK's signature over the remote device list.
    "sig_remote_devices": "orch/v2/sig/remote-devices|",
    "aad_hub_share": "orch/v2/hub-share-aad|",
    "h_share_item": "orch/v2/hub-share-item|",
}

L: dict[str, bytes] = {k: v.encode("ascii") for k, v in {**LABELS, **PLANNED_LABELS}.items()}

SIGNATURE_LABEL_KEYS = tuple(k for k in LABELS if k.startswith("sig_"))


def signature_labels() -> tuple[bytes, ...]:
    """Every protocol signature label (``sig_*``, plus ``orch/v2/ws-envelope|`` and ``orch/v2/publish|``, which are
    signature domains too) and the event labels of :data:`orch.canon.LABELS`: the only prefixes a custody backend
    signs."""
    from orch import canon

    proto = [L[k] for k in SIGNATURE_LABEL_KEYS]
    events = [canon.LABELS[k].encode("ascii") for k in canon.LABELS if k.startswith("sig_")]
    return tuple(proto + events)
