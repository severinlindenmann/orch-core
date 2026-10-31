"""M4 S0: core replays the ``hub_where`` cases of the shared ``contracts/hub/vectors_hub_backup.json`` against its own
validator ``orch.hubclient.where``. The other checks of that file (backup file, tar, restore floor) are the hub's;
they are named here so a new check kind cannot go unnoticed."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import orch
from orch.crypto.encoding import unb64u
from orch.hubclient import HubRefused
from orch.hubclient.publish_where import canonical_origin, check_publish_where
from orch.hubclient.where import check_hub_where

from .vec import CONTRACT_DIR

DOC = json.loads((CONTRACT_DIR / "vectors_hub_backup.json").read_text())
HUB_ONLY = {"bk_key", "open_backup", "tar", "file_list", "restore_floor"}
WHERE = [c for c in DOC["cases"] if c["check"] == "hub_where"]
PUBLISH_WHERE = [c for c in DOC["cases"] if c["check"] == "publish_where"]
ORIGINS = [c for c in DOC["cases"] if c["check"] == "canonical_origin"]
#: #1838: the cases a hub.publish_where verifier must not lose (each name is a case of the shared file)
REQUIRED_PUBLISH_WHERE = {
    "pw_good", "pw_good_dev", "pw_wrong_hub_id", "pw_bad_signature", "pw_signed_under_another_op_key",
    "pw_dev_true_at_a_real_pin", "pw_dev_false_at_a_dev_pin", "pw_loopback_http_dev_false_refused",
    "pw_loopback_http_dev_true_accepted", "pw_server_private_ip_10_refused", "pw_server_local_name_refused",
    "pw_server_localhost_refused", "pw_server_userinfo_refused", "pw_server_path_refused", "pw_server_query_refused",
    "pw_server_fragment_refused", "pw_share_private_ip_10_refused", "pw_share_local_name_refused",
    "pw_idn_canonicalised_to_the_a_label", "pw_default_port_and_trailing_dot_canonicalised",
    "pw_lower_seq_refused", "pw_equal_seq_byte_identical_accepted", "pw_equal_seq_other_content_refused",
    "pw_higher_seq_accepted", "where_publish_where_ok", "where_publish_where_and_legacy_publish",
    "where_publish_ok_https", "origin_private_ip_10_refused", "origin_idn_a_label",
}  # fmt: skip


def test_no_unknown_check_kind() -> None:
    assert {c["check"] for c in DOC["cases"]} == HUB_ONLY | {"hub_where", "publish_where", "canonical_origin"}
    assert len(WHERE) >= 35 and len(PUBLISH_WHERE) >= 100 and len(ORIGINS) >= 100
    assert {"where_ok", "where_five_entries", "where_bad_addr_no_port", "where_unknown_kind", "where_name_61_long",
            "where_name_control_and_bidi_stripped",
            "where_name_surrogate_private_use_unassigned_stripped"} <= {c["name"] for c in WHERE}  # fmt: skip
    names = {c["name"] for c in DOC["cases"]}
    assert REQUIRED_PUBLISH_WHERE <= names, REQUIRED_PUBLISH_WHERE - names


@pytest.mark.parametrize("case", WHERE, ids=[c["name"] for c in WHERE])
def test_where_case(case) -> None:
    if "error" in case["expect"]:
        with pytest.raises(HubRefused) as e:
            check_hub_where(case["args"]["answer"])
        assert e.value.code == case["expect"]["error"]
    else:
        assert check_hub_where(case["args"]["answer"]) == case["expect"]["ok"]


@pytest.mark.parametrize("case", PUBLISH_WHERE, ids=[c["name"] for c in PUBLISH_WHERE])
def test_publish_where_case(case) -> None:
    a = case["args"]
    kw = {"op_pub": unb64u(a["op_pub"], 65), "hub_id": a["hub_id"], "dev": a["dev"], "held": a.get("held")}
    if "error" in case["expect"]:
        with pytest.raises(HubRefused) as e:
            check_publish_where(a["signed"], **kw)
        assert e.value.code == case["expect"]["error"]
    else:
        assert check_publish_where(a["signed"], **kw) == case["expect"]["ok"]


@pytest.mark.parametrize("case", ORIGINS, ids=[c["name"] for c in ORIGINS])
def test_canonical_origin_case(case) -> None:
    a = case["args"]
    if "error" in case["expect"]:
        with pytest.raises(HubRefused) as e:
            canonical_origin(a["origin"], a["dev"])
        assert e.value.code == case["expect"]["error"]
    else:
        assert canonical_origin(a["origin"], a["dev"]) == case["expect"]["ok"]


def test_the_legacy_publish_string_is_never_returned() -> None:
    ok = {"addresses": [{"kind": "private", "link": "100.64.1.2:8105"}], "name": "H"}
    for pub in ("https://share.example.com", "http://127.0.0.1:8102"):
        assert check_hub_where({**ok, "publish": pub}) == ok
    with pytest.raises(HubRefused):
        check_hub_where({**ok, "publish": "http://evil.example.com"})  # still shape-checked


def test_the_label_is_the_documented_one() -> None:
    from orch.hubclient.publish_where import SIG_PUBLISH_WHERE

    assert SIG_PUBLISH_WHERE == DOC["labels"]["sign_publish_where"].encode() == b"orch/v2/hub-publish-where|"


def _core_labels() -> set[str]:
    """Every ``orch/v2/...`` string literal in core's source (kdf labels have no ``|``, the rest end in it)."""
    found: set[str] = set()
    for f in Path(orch.__file__).parent.rglob("*.py"):
        found |= set(re.findall(r"""["'](orch/v2/[A-Za-z0-9/_.-]+\|?)["']""", f.read_text()))
    return found


def _clashes(mine: str, others: set[str]) -> list[str]:
    return sorted(o for o in others if o != mine and (o.startswith(mine) or mine.startswith(o)))


def test_backup_labels_are_prefix_free_with_every_core_label() -> None:
    """S0 review (info): the two hub-backup labels are new and prefix-free with every label core uses (the hub test
    checks them against the hub's own table)."""
    core = _core_labels()
    assert len(core) > 100 and {"orch/v2/seal-aad|", "orch/v2/sig/device-cert|", "orch/v2/seal"} <= core
    mine = [DOC["labels"]["sign_header"], DOC["labels"]["aad"]]
    for m in mine:
        assert m.startswith("orch/v2/") and m.endswith("|") and m not in core
        assert not _clashes(m, core), m
    assert _clashes("orch/v2/seal", core)  # the check itself bites: a label that is a prefix of another is found
