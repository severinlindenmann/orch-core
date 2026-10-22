"""Invariant: the page, the dashboard server and the signer accept exactly the same material keys (#740, #784, #790).

The sign-request checks exist three times: core's signer (`orch.host.signer` / `orch.host.review`), the dashboard
server (`orch_dashboard.adapters.handoff`) and the page (`src/api/handoff.ts`, read here as text). Each hand-kept list
they share is asserted equal here, so a core change that adds a key or a type fails this test instead of an e2e run.
"""

import inspect
import re
from pathlib import Path

from orch.host import review, signer

from orch_dashboard.adapters import handoff as server

PAGE = (Path(__file__).resolve().parents[2] / "src" / "api" / "handoff.ts").read_text()


def ts_strings(text: str) -> set[str]:
    return set(re.findall(r"'([^']*)'", text))


def ts_const(name: str) -> str:
    """The initializer text of one top-level `const NAME ... = ...` of the page (up to the line that closes it)."""
    m = re.search(rf"^(?:export )?const {name}\b[^=]*=\s*(.*?)\n(?=\S)", PAGE, re.S | re.M)
    assert m, name
    return m.group(1)


def page_material_keys() -> dict[str, tuple[set[str], set[str]]]:
    rows = re.findall(r"(\w+): \[\[([^\]]*)\], \[([^\]]*)\]\]", ts_const("MATERIAL_KEYS"))
    return {kind: (ts_strings(required), ts_strings(allowed)) for kind, required, allowed in rows}


def test_material_keys_page_server_signer():
    page = page_material_keys()
    assert set(page) == set(server.MATERIAL_KEYS) == {"gate", "question", "payload"}
    for kind in page:
        assert page[kind] == tuple(server.MATERIAL_KEYS[kind]) == signer._MATERIAL_KEYS[kind], kind


def test_statement_ops_page_server_signer():
    ops = ("ac.accept", "ac.unaccept")
    assert ts_strings(ts_const("STATEMENT_OPS")) == set(server.STATEMENT_OPS) == set(ops)
    # the signer names them inline: its refusal of a statement under any other op uses the same tuple
    source = inspect.getsource(signer._person_op)
    assert '"statement" in prepared["material"] and op not in ("ac.accept", "ac.unaccept")' in source


def test_review_envelope_page_server_core():
    assert ts_strings(ts_const("ENVELOPE")) == set(server.ENVELOPE) == set(review.ENVELOPE)


def test_gate_gen_types_page_server_core():
    assert ts_strings(ts_const("GATE_GEN_ENVELOPE")) == set(server.GATE_EVENTS) == set(review.GATE_TYPES)


def test_fingerprint_view_server_core():
    """What the fingerprint covers (no sig, auth, actor.device, a grant's secret_hash): the server's equals core's.
    The page's is pinned by the shared vectors in src/api/handoff.test.ts."""
    actor = {"kind": "person", "id": "p_" + "1" * 32, "device": "d_" + "2" * 32}
    for event in (
        {"type": "log.added", "id": "e1", "actor": actor, "auth": "passphrase", "sig": "x", "text": "t"},
        {"type": "grant.issued", "id": "e2", "actor": actor, "auth": None, "secret_hash": "h", "hours": 8},
    ):
        assert server.stripped(event) == review.fingerprint_view(event)
        digest = "sha256:" + "ab" * 32
        assert server.fingerprint(digest, [event], "op") == review.fingerprint(digest, [event], "op")
