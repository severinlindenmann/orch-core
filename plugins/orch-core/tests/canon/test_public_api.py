"""The export surface of orch.canon is pinned: adding or removing a name is a deliberate change of this list."""

import orch.canon as canon

GROUPS = {
    "text": [
        "BIDI_CONTROLS",
        "UNICODE_VERSION",
        "Suspect",
        "TextError",
        "check_text",
        "clean",
        "clean_line",
        "is_clean_text",
        "nfc",
        "normalize_text",
        "show_invisible",
        "suspicious",
    ],
    "cj": ["JcsError", "dumps", "loads_strict", "validate"],
    "hashes": [
        "ARTIFACT_KINDS",
        "GATES",
        "GATE_KEYS",
        "HASH_V",
        "HashError",
        "LABELS",
        "artifact_digest",
        "canonical_policy",
        "check_hash_v",
        "canonical_repo_identity",
        "check_repo_identity",
        "cj_checked",
        "format_hash",
        "gate_hash",
        "grant_secret_hash",
        "parse_hash",
        "people_hash",
        "policy_hash",
        "question_hash",
        "question_id",
        "same_repo_identity",
        "section_hash",
        "value_hash",
    ],
    "chain_and_signing": [
        "CONTRACT",
        "SUITE",
        "ChainError",
        "check_chain",
        "event_head",
        "event_line",
        "host_signing_bytes",
        "log_head",
        "parse_event_line",
        "person_signing_bytes",
        "signed_context",
    ],
}


def test_all_is_pinned():
    expected = sorted(n for names in GROUPS.values() for n in names)
    assert sorted(canon.__all__) == expected
    assert len(set(canon.__all__)) == len(canon.__all__)


def test_every_exported_name_exists():
    for n in canon.__all__:
        assert hasattr(canon, n), n


def test_no_general_serialiser_or_old_names():
    for old in ("dumps_general", "sha256_hex", "GATE_KEYS_V1", "SUPPORTED_HASH_V", "is_normalized"):
        assert not hasattr(canon, old), old
