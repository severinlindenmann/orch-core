"""Cross-consumer schedule shape and signature contract."""

import json
from pathlib import Path

import pytest

from orch import canon, crypto, schema
from orch.click.protocol import risk_lines
from orch.custody.describe import describe_payload
from orch.host import signcontent

V = json.loads((Path(__file__).resolve().parents[4] / "contracts/core/schedule_set_vectors.json").read_text())


@pytest.mark.parametrize("v", V["vectors"], ids=lambda v: v["name"])
def test_schedule_vectors(v):
    event = v["event"]
    schema.validate("event", event, log="ticket")
    signed = canon.person_signing_bytes(V["workspace_id"], V["log"], event)
    assert signed.hex() == v["signed_hex"]
    assert crypto.verify(bytes.fromhex(V["public_key"]), bytes.fromhex(v["signature"]), signed) == v["valid"]
    lines = describe_payload(signed)
    content = signcontent.build(
        {"op": "schedule.add", "events": [event], "material": {"kind": "payload"}}, verified=True
    )
    if event.get("target") == "new":
        for text in ("creates a NEW ticket every run", "Mail", "once on", event["ends"]):
            assert text in content["headline"]
            assert text in str(lines)
        review = risk_lines("schedule.add", [event])
        assert "each new ticket" in review[1]
        assert content["headline"] == review[0]


def test_schedule_signed_shapes_differ():
    assert V["vectors"][0]["signed_hex"] != V["vectors"][1]["signed_hex"]


@pytest.mark.parametrize(
    "extra",
    [
        {"target": "unknown"},
        {"target": None},
        {"title": "Mail"},
        {"target": "new"},
        {"target": "new", "title": ""},
        {"target": "new", "title": "x" * 101},
        {"unexpected": True},
    ],
)
def test_schedule_schema_refuses_bad_shapes(extra):
    with pytest.raises(schema.SchemaError):
        schema.validate("event", dict(V["vectors"][0]["event"], **extra), log="ticket")
