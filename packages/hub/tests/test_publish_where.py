"""#1838 step A: ``hub.publish_where`` beyond the replayed vectors (``test_hub_backup_vectors.py`` replays every case of the
shared file): the signer the hub will use, the origin rule's idempotence, and the legacy ``publish`` string."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract.labels import HUB_LABELS
from orch_hub.contract.publish_where import canonical_origin, check_publish_where
from orch_hub.contract.where import check_hub_where

DOC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_hub_backup.json").read_text())
CASES = {c["name"]: c for c in DOC["cases"]}
OP = S.key_from_seed(S.fake_seed("hub-backup/op"))
GOOD = CASES["pw_good"]["args"]
WHERE_OK = {"addresses": [{"kind": "private", "link": "100.64.1.2:8105"}], "name": "H"}


def test_the_label_is_the_documented_one() -> None:
    assert HUB_LABELS["sig_hub_publish_where"] == "orch/v2/hub-publish-where|" == DOC["labels"]["sign_publish_where"]
    assert H.LABEL_OF_KIND["hub.publish_where"] == "sig_hub_publish_where"
    assert KS.OpSigner.KINDS["hub.publish_where"] == "sig_hub_publish_where"
    for cls in (KS.RootSigner, KS.UkSigner, KS.ChannelSigner, KS.WskSigner):
        assert "hub.publish_where" not in cls.KINDS


def test_the_op_signer_signs_what_the_checker_accepts() -> None:
    o = GOOD["signed"]["o"]
    signed = KS.OpSigner(OP).sign_object(o)  # a real, randomised signature
    assert check_publish_where(signed, op_pub=S.public_bytes(OP), hub_id=o["hub_id"], dev=False) == o
    with pytest.raises(H.Refused) as e:
        check_publish_where(
            signed,
            op_pub=S.public_bytes(OP),
            hub_id=o["hub_id"],
            dev=False,
            held={**signed, "o": {**o, "seq": o["seq"] + 1}},
        )
    assert e.value.code == "stale_seq"


def test_only_the_op_key_signs_it() -> None:
    root = S.key_from_seed(S.fake_seed("hub-backup/root"))
    with pytest.raises(KS.KeyUseError):
        KS.RootSigner(root).sign_object(GOOD["signed"]["o"])


@pytest.mark.parametrize(
    "name", [n for n, c in CASES.items() if c["check"] == "canonical_origin" and "ok" in c["expect"]]
)
def test_canonical_origin_is_idempotent(name: str) -> None:
    a = CASES[name]["args"]
    once = canonical_origin(a["origin"], a["dev"])
    assert canonical_origin(once, a["dev"]) == once


def test_the_legacy_publish_string_is_never_returned() -> None:
    for pub in ("https://share.example.com", "http://127.0.0.1:8102"):
        assert check_hub_where({**WHERE_OK, "publish": pub}) == WHERE_OK
    with pytest.raises(H.Refused):
        check_hub_where({**WHERE_OK, "publish": "http://evil.example.com"})  # still shape-checked
    signed = GOOD["signed"]
    assert check_hub_where({**WHERE_OK, "publish": "https://x.example.com", "publish_where": signed}) == {
        **WHERE_OK,
        "publish_where": signed,
    }
