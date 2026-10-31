"""#1455: the go-live approval label is the DEVICE key's (dk_sig) and nobody else's."""

from __future__ import annotations

from orch.crypto.labels import L
from orch.custody import base

LABEL = L["sig_hub_publish_approval"]


def test_only_the_device_key_signs_the_publish_approval_label():
    assert base.label_allowed(LABEL + b"{}", base.DEVICE_KEY_LABELS)
    for role in ("person", "workspace", "primary", "remote-presence", "kx"):
        assert not base.label_allowed(LABEL + b"{}", base.ROLE_LABELS[role]), role
    assert not base.label_allowed(LABEL + b"{}", base.PERSON_KEY_LABELS)
    assert not base.label_allowed(LABEL + b"{}", base.HOST_LABELS)


def test_the_label_sets_stay_prefix_free_with_it():
    every = [*base.DEVICE_KEY_LABELS, *base.PERSON_KEY_LABELS, *base.HOST_LABELS]
    for a in every:
        for b in every:
            assert a == b or not a.startswith(b), (a, b)
