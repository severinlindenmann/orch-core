"""H13-0: the two planned host-local labels (orch-v2-host 11A.16.2, 11A.16.6, 11A.16.11 row H13-0).

``kdf_host_drop_recovery`` (``orch/v2/host-drop-recovery|``) keys the host-only recovery envelope (D7, Q4);
``sig_drop_handoff`` (``orch/v2/sig/drop-handoff|``) is the terminal hand-off label (O1; signed from H13b-4). Neither is
a relay format, so neither is in the relay corpus or in :data:`orch.crypto.labels.LABELS` (which mirrors the corpus).

"No label is a prefix of another": every ``|``-terminated domain label core knows (protocol, planned, canon/F1 and
the vendored corpus) is pairwise prefix-free, and each planned label is prefix-free against **every** label, the
unterminated HKDF ``info`` labels included. (The unterminated ``kdf_*`` labels are prefixes of each other by
construction, e.g. ``orch/v2/push`` and ``orch/v2/push-msg``; they are only ever used as ``label "|" field ...`` HKDF
contexts, and the planned labels are kept clear of them as well.)"""

from __future__ import annotations

import json
from pathlib import Path

from orch import canon
from orch.crypto import labels

F1 = Path(__file__).parent.parent / "vectors" / "f1" / "labels.json"
PLANNED = {
    "kdf_host_drop_recovery": "orch/v2/host-drop-recovery|",
    "sig_drop_handoff": "orch/v2/sig/drop-handoff|",
    "sig_hub_binding": "orch/v2/sig/hub-binding|",  # V3 C2a: what the WSK signs for a hub
    "sig_hub_unbound": "orch/v2/sig/hub-unbound|",
    "sig_hub_link": "orch/v2/sig/hub-link|",
    "sig_hub_link_auth": "orch/v2/sig/hub-link-auth|",
    "sig_publish_hub": "orch/v2/publish-hub|",  # V3 C7: publish through the hub
    "sig_space_item": "orch/v2/sig/space-item|",  # V3 C13: shared spaces (hub contract 10.4)
    "sig_space_accept": "orch/v2/sig/space-accept|",
    "h_space_members": "orch/v2/hub-space-members|",
    "h_sk_commit": "orch/v2/hub-sk-commit|",
    "aad_space": "orch/v2/space-aad|",
    "sig_share_item": "orch/v2/sig/share-item|",  # V3 C6: selective share_put (hub contract 10.2b)
    "sig_device_rotate": "orch/v2/sig/device-rotate|",  # #1097: the person key's device rotation
    "sig_hub_owner_proof": "orch/v2/sig/hub-owner-proof|",  # #1198 C9: the owner proof of a hub step
    "aad_hub_share": "orch/v2/hub-share-aad|",
    "h_share_item": "orch/v2/hub-share-item|",
}


def _every() -> dict[str, str]:
    from tests.wsproto.corpus import VECTORS

    f1 = json.loads(F1.read_text(encoding="utf-8"))["labels"]
    out: dict[str, str] = {}
    for src, table in (
        ("corpus", VECTORS["labels"]),
        ("protocol", labels.LABELS),
        ("planned", labels.PLANNED_LABELS),
        ("f1", f1),
        ("canon", canon.LABELS),
    ):
        for k, v in table.items():
            out[f"{src}:{k}"] = v
    return out


def test_the_planned_labels_and_their_literals():
    assert labels.PLANNED_LABELS == PLANNED
    for k, v in PLANNED.items():
        assert labels.L[k] == v.encode("ascii")


def test_planned_labels_are_not_relay_labels():
    from tests.wsproto.corpus import VECTORS

    assert not set(PLANNED) & set(labels.LABELS)
    assert not set(PLANNED) & set(VECTORS["labels"])
    assert not set(PLANNED.values()) & set(VECTORS["labels"].values())


def test_each_planned_label_is_prefix_free_against_every_label():
    every = _every()
    for key, lab in PLANNED.items():
        for name, other in every.items():
            if name == f"planned:{key}":
                continue
            assert other != lab, (key, name)
            assert not other.startswith(lab) and not lab.startswith(other), (key, name, other)


def test_every_terminated_label_is_prefix_free_across_all_sources():
    terminated = {v for v in _every().values() if v.endswith("|")}
    for a in terminated:
        for b in terminated:
            assert a == b or not b.startswith(a), (a, b)


def test_one_value_per_key_across_sources():
    seen: dict[str, str] = {}
    for name, v in _every().items():
        key = name.split(":", 1)[1]
        assert seen.setdefault(key, v) == v, name


def test_handoff_label_is_not_yet_signable():
    """H13b-4 adds it to DEVICE_KEY_LABELS with the signer's material kinds; until then no key role signs it."""
    from orch.custody.base import ROLE_LABELS

    raw = labels.L["sig_drop_handoff"]
    assert all(raw not in v for v in ROLE_LABELS.values())
    assert raw not in labels.signature_labels()
