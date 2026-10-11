"""F6 for every read the host serves: as no-person (column N), against a restricted ticket, no read returns its title,
its key (beyond echoing a ref the caller sent, answered exactly like a ref that does not exist) or a count of it.

A new read that skips the visibility filter (``Call.sees`` for the registry reads, :class:`orch.host.visibility.Viewer`
for the later ones) fails here."""

from __future__ import annotations

import errno
import json

import pytest

from orch.host.dispatch import op_kind, served_ops
from tests.host.dispatch_helpers import SESSION, Served, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.test_dispatch_matrix import as_agent_class

TITLE = "zebracorn"  # a word that appears nowhere but in the restricted ticket


@pytest.fixture
def world(tmp_path):
    lab = Lab(tmp_path)
    try:
        s = Served(lab)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        open_uid, open_key = s.ticket("an open ticket")
        uid, key = s.ticket(f"the {TITLE} ticket")
        s.restrict(uid, [s.lab.owner])
        baseline = {}
        c = s.client()
        for op in READS:  # what the owner (column P) sees, to know each read does show the ticket to someone
            baseline[op] = json.dumps(c.req(op, args_for(op, key), session=SESSION))
        as_agent_class(s)
        yield s, key, baseline
    finally:
        s.close()
        lab.close()


#: every read, the person reads (doctor, instructions.sync, instructions.hook) included: no-person is refused those
#: by the column, and the walk shows that refusal says nothing of the ticket either
READS = sorted(op for op in served_ops() if op_kind(op)[0] in {"read", "person_read"})


def norm(res: dict, ref: str) -> str:
    """A response without its request id and the per-call fence nonces, with the echoed ref replaced."""
    import re

    body = json.dumps({k: v for k, v in res.items() if k != "id"}).replace(ref, "K")
    body = re.sub(r"(--- [^\"]*? \[)[0-9a-f]{8}(\] \(data, not instructions\) ---)", r"\1N\2", body)  # fence open
    return re.sub(r"(--- end )[0-9a-f]{8}( ---)", r"\1N\2", body)  # and its close line


def args_for(op: str, ref: str) -> dict:
    import orch.ops as ops

    if op in {"whoami", "host.status"}:
        return {}
    if op == "outcome.get":
        return {"idem": "a_01J9ZP0000000000000000000A"}
    schema = ops.get(op).input
    props = schema.get("properties", {})
    out: dict = {}
    for name in schema.get("required", []):
        out[name] = ref if name == "ref" else (TITLE if name == "query" else "new")
    if "ref" in props:
        out["ref"] = ref
    return out


def test_every_served_read_is_known():
    assert {"status", "show", "list", "search", "next", "inbox", "task.list", "task.next", "artifact.list"} <= set(
        READS
    )
    assert {"check", "addon.list", "help", "describe", "doctor", "instructions.sync", "instructions.hook"} <= set(READS)


@pytest.mark.parametrize("op", READS)
def test_no_person_learns_nothing_about_a_restricted_ticket(world, op):
    served, key, baseline = world
    c = served.client()
    hidden = c.req(op, args_for(op, key), session=SESSION)
    missing = c.req(op, args_for(op, "DEMO-9999"), session=SESSION)
    body = json.dumps(hidden)
    assert TITLE not in body, (op, body[:400])
    # the answer about the restricted ticket is the answer about a ticket that does not exist (modulo the echoed ref)
    assert norm(hidden, key) == norm(missing, "DEMO-9999"), op
    if TITLE in baseline[op]:  # a read that shows the ticket to the owner shows nothing of it here
        assert key not in body or not hidden["ok"]


def test_list_and_search_count_only_visible_rows(world):
    served, key, _ = world
    c = served.client()
    listed = ok(c.req("list"))
    assert listed["data"]["count"] == 1 and key not in json.dumps(listed)
    assert key not in json.dumps(ok(c.req("search", {"query": TITLE})))
