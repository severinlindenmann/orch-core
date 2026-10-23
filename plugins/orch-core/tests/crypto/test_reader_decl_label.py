"""H12d-1: the reader-declaration label ``orch/v2/sig/reader-decl|`` (``sig_reader_decl``, orch-v2-host §0.8.6 item
9.3, 9.11). An F1 label (it lives in :data:`orch.canon.LABELS`, pinned by ``tests/vectors/f1/labels.json``), signed only
by a device key, and prefix-free against every label core knows and every label of the vendored relay corpus."""

from __future__ import annotations

import json
from pathlib import Path

from orch import canon
from orch.crypto import labels

VECTORS = Path(__file__).parent.parent / "vectors"
LABEL = "orch/v2/sig/reader-decl|"


def _corpus_labels() -> dict[str, str]:
    """The vendored relay corpus (``tests/wsproto/vectors/vectors_v2.json.xz``, checked against SOURCE)."""
    from tests.wsproto.corpus import VECTORS as RELAY_VECTORS

    return RELAY_VECTORS["labels"]


def _every_label() -> dict[str, str]:
    corpus = _corpus_labels()
    f1 = json.loads((VECTORS / "f1" / "labels.json").read_text(encoding="utf-8"))["labels"]
    out: dict[str, str] = {}
    for source, table in (("corpus", corpus), ("protocol", labels.LABELS), ("f1", f1), ("canon", canon.LABELS)):
        for k, v in table.items():
            out[f"{source}:{k}"] = v
    return out


def test_the_label_literal_and_its_key():
    assert canon.LABELS["sig_reader_decl"] == LABEL
    f1 = json.loads((VECTORS / "f1" / "labels.json").read_text(encoding="utf-8"))["labels"]
    assert f1["sig_reader_decl"] == LABEL


def test_the_label_is_prefix_free_against_every_known_label():
    every = _every_label()
    for name, other in every.items():
        if other == LABEL:
            assert name.endswith(":sig_reader_decl"), name  # the same string only under its own key
            continue
        assert not other.startswith(LABEL) and not LABEL.startswith(other), (name, other)


def test_the_relay_corpus_names_it_the_same_way_when_present():
    corpus = _corpus_labels()
    assert corpus.get("sig_reader_decl", LABEL) == LABEL
    assert LABEL not in {v for k, v in corpus.items() if k != "sig_reader_decl"}


def test_only_a_device_key_signs_it():
    from orch.custody.base import DEVICE_KEY_LABELS, HOST_LABELS, PERSON_KEY_LABELS, label_allowed

    raw = LABEL.encode("ascii")
    assert raw in DEVICE_KEY_LABELS
    assert raw not in PERSON_KEY_LABELS and raw not in HOST_LABELS
    assert label_allowed(raw + b"{}", DEVICE_KEY_LABELS)
    assert not label_allowed(raw + b"{}", PERSON_KEY_LABELS + HOST_LABELS)
    assert raw in labels.signature_labels()
