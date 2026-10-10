"""Protocol domain-separation labels (orch-relay protocol-v2 §3). One place; ``tests/crypto/test_vectors.py`` checks
the table against ``vectors_v2.json`` and that the signature, AAD and hash labels are prefix-free.

The event-signing labels of ticket-format §5.3 live in :data:`orch.canon.LABELS`; use those for events.
"""

from __future__ import annotations

__all__ = ["L", "LABELS", "SIGNATURE_LABEL_KEYS", "signature_labels"]

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
    "h_dek_commit": "orch/v2/dek-commit|",
    "h_card_key_commit": "orch/v2/card-key-commit|",
    "h_drop_parent": "orch/v2/drop-parent|",
    "h_ws_envelope": "orch/v2/ws-envelope-hash|",
    "h_question": "orch/v2/question|",
    "h_assert": "orch/v2/assert|",
    "h_webauthn_reg": "orch/v2/webauthn-reg|",
    "mac_enroll": "orch/v2/enroll|",
}

L: dict[str, bytes] = {k: v.encode("ascii") for k, v in LABELS.items()}

SIGNATURE_LABEL_KEYS = tuple(k for k in LABELS if k.startswith("sig_"))


def signature_labels() -> tuple[bytes, ...]:
    """Every protocol signature label (``sig_*``, plus ``orch/v2/ws-envelope|`` and ``orch/v2/publish|``, which are
    signature domains too) and the event labels of :data:`orch.canon.LABELS`: the only prefixes a custody backend
    signs."""
    from orch import canon

    proto = [L[k] for k in SIGNATURE_LABEL_KEYS]
    events = [canon.LABELS[k].encode("ascii") for k in canon.LABELS if k.startswith("sig_")]
    return tuple(proto + events)
