"""F6 for every read the host serves: as no-person (column N), against a restricted ticket, no read returns its title,
its key (beyond echoing a ref the caller sent, answered exactly like a ref that does not exist) or a count of it.

A new read that skips the visibility filter (``Call.sees`` for the registry reads, :class:`orch.host.visibility.Viewer`
for the later ones) fails here."""

from __future__ import annotations

import errno
import json
import time

import pytest

from orch.host.dispatch import PERSON_GRANT_REGISTRY_READS, op_kind, served_ops
from tests.host.dispatch_helpers import SESSION, Served, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.test_dispatch_matrix import as_agent_class

TITLE = "zebracorn"  # a word that appears nowhere but in the restricted ticket


@pytest.fixture(scope="module")
def _world(tmp_path_factory):
    """The host, its workspace and the baseline walk, built once for the module: every test here only reads. The
    per-test state (the page snapshot store) is swapped by the autouse ``_fresh_snapshot_store``."""
    lab = Lab(tmp_path_factory.mktemp("reads"))
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
            if op != "wait":  # it parks for its timeout; its visibility is the ticket_visible precondition's
                baseline[op] = json.dumps(c.req(op, args_for(op, key), session=SESSION))
        for op in ("records.snapshot", "records.export_check", "records.status"):  # valid arguments: a real baseline
            assert json.loads(baseline[op])["ok"] is True, baseline[op]
        as_agent_class(s)
        yield s, key, baseline
    finally:
        s.close()
        lab.close()


@pytest.fixture
def world(_world):
    """The module's world with per-test state reset around it: the connections a test opened are closed (and the host
    has released them), and the host's connection-rate bucket is refilled (it caps attempts per UID per second)."""
    served = _world[0]
    limits = served.h.host.limits
    before = len(served.clients)
    kept = sum(limits.connections.values())
    limits.attempts.clear()
    try:
        yield _world
    finally:
        for c in served.clients[before:]:
            c.close()
        del served.clients[before:]
        deadline = time.monotonic() + 5
        while sum(limits.connections.values()) > kept and time.monotonic() < deadline:
            time.sleep(0.005)


#: every read, the person reads (doctor, instructions.sync, instructions.hook) included: no-person is refused those
#: by the column, and the walk shows that refusal says nothing of the ticket either
#: (``term.preview`` is not a registry op: its ticket is checked by ``Viewer.ticket``, tests/host/test_terms.py)
READS = sorted(
    op
    for op in served_ops()
    if (op_kind(op)[0] in {"read", "person_read"} or op in PERSON_GRANT_REGISTRY_READS)
    and op not in ("term.preview", "agent.preview", "run.transcript", "delegation.list", "worktree.diff", "run.sources")
)  # agent.preview (H11c) is not a registry op either: tests/host/test_agents.py; run.transcript: test_transcripts.py


RECORDS_ARGS = {
    "records.snapshot": {},
    "records.export_check": {
        "sha": "a" * 40,
        "url": "https://example.org/r.git",
        "ref": "refs/heads/main",
        "history_convenience": False,
    },
    "records.remote_private": {"on": False},
    "records.status": {},
    "records.note": {"pushed": False},
}

#: the dashboard reads (§0.8, §0.8.5) and the arguments that aim each at the ticket ``ref`` (or at everything)
DASHBOARD_READS = {
    "ticket.query": lambda ref: {},
    "ticket.get": lambda ref: {"ref": ref},
    "needs": lambda ref: {},
    "activity.list": lambda ref: {"ticket": ref},
    "workspace.show": lambda ref: {},
    "member.list": lambda ref: {},
    "session.list": lambda ref: {},
    "grant.list": lambda ref: {},
    "workspace.settings": lambda ref: {},
    "ticket.text": lambda ref: {"ref": ref, "seq": 1, "field": "text"},
    "reader.list": lambda ref: {},  # H12d-2
    "gate.input": lambda ref: {"ref": ref, "gate": "requirements"},  # B10 Part B
    "phone.requests": lambda ref: {},
    "phone.status": lambda ref: {},
}


def unserved(res: dict) -> bool:
    """A dashboard read whose handler is still a placeholder (another implementer's file): ``unknown_command``."""
    return not res.get("ok") and res.get("error", {}).get("code") == "unknown_command"


def norm(res: dict, ref: str) -> str:
    """A response without its request id and the per-call fence nonces, with the echoed ref replaced."""
    import re

    body = json.dumps({k: v for k, v in res.items() if k != "id"}).replace(ref, "K")
    body = re.sub(r"(--- [^\"]*? \[)[0-9a-f]{8}(\] \(data, not instructions\) ---)", r"\1N\2", body)  # fence open
    return re.sub(r"(--- end )[0-9a-f]{8}( ---)", r"\1N\2", body)  # and its close line


def args_for(op: str, ref: str) -> dict:
    import orch.ops as ops

    if op in {"whoami", "host.status", "hub.status", "share.rules", "share.preview"}:  # share.*: owner-only (C6)
        return {}
    if op == "hub.link":  # run-9: reads a connect link; nothing to do with tickets (the port refuses at once)
        return {"link": "http://127.0.0.1:1/c#h1." + "a" * 32 + ".x"}
    if op == "hub.handoff":  # V3 C11: a person read that names a ticket (standalone here: hub.not_joined)
        return {"kind": "status", "ticket": ref}
    if op == "outcome.get":
        return {"idem": "a_01J9ZP0000000000000000000A"}
    if op == "sub.open":  # H6a: a ticket topic goes through Viewer.ticket
        return {"topics": [f"ticket:{ref}"]}
    if op == "sub.close":
        return {"sub": "sub_0"}
    if op == "wait":  # parks on the bus: a short timeout for the owner's baseline
        return {"ref": ref, "timeout": 1}
    if op in DASHBOARD_READS:  # H6b: not registry ops, so no registry schema to read the arguments from
        return DASHBOARD_READS[op](ref)
    if op == "addon.preview":  # names an add-on, never a ticket (a built-in one, so that every call answers)
        return {"name": "usage"}
    if op == "diff":  # through Viewer.ticket, then Viewer.linked_repo
        return {"ref": ref}
    if op == "artifact.get":  # H4a2: through Viewer.ticket, then Viewer.artifact
        return {"ref": ref, "name": "proof.txt", "offset": 0}
    if op == "blob.put":  # H4a2: blobs are per principal and name no ticket
        return {"offset": 0, "b64": "", "last": False, "size": 1, "upload_key": "a_01J9ZP0000000000000000000B"}
    if op == "blob.drop":
        return {"blob": "b_" + "A" * 26}
    if op == "workdir.get":  # r3 part C: names a ticket by `ticket`
        return {"ticket": ref}
    if op in RECORDS_ARGS:  # H11e: the records ops name no ticket
        return RECORDS_ARGS[op]
    if op == "grant.deposit_status":  # H11c: names a deposit, never a ticket
        return {"deposit_id": "01J9ZP0000000000000000000C"}
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
    if op == "follow_up.show":
        pytest.skip("names a follow-up id, not a ticket, and is gated by the Schedules add-on")
    served, key, baseline = world
    c = served.client()
    hidden = c.req(op, args_for(op, key), session=SESSION)
    missing = c.req(op, args_for(op, "DEMO-9999"), session=SESSION)
    if op in DASHBOARD_READS and unserved(hidden):
        pytest.skip(f"{op} has no handler yet (the assertion below runs once it is served)")
    body = json.dumps(hidden)
    assert TITLE not in body, (op, body[:400])
    # the answer about the restricted ticket is the answer about a ticket that does not exist (modulo the echoed ref)
    assert norm(hidden, key) == norm(missing, "DEMO-9999"), op
    if TITLE in baseline.get(op, ""):  # a read that shows the ticket to the owner shows nothing of it here
        assert key not in body or not hidden["ok"]


def test_every_dashboard_read_is_covered_by_the_walk():
    served = set(served_ops())
    assert set(DASHBOARD_READS) <= served and set(DASHBOARD_READS) >= served & {
        "ticket.query", "ticket.get", "needs", "activity.list", "workspace.show", "member.list", "session.list",
        "grant.list", "workspace.settings", "ticket.text",
    }  # fmt: skip
    walked = {op for op in READS if op in DASHBOARD_READS}
    assert {"ticket.query", "ticket.get", "needs", "activity.list", "workspace.show", "member.list"} <= walked


def test_ticket_query_and_needs_show_the_visible_ticket_to_the_owner_and_nothing_to_no_person(world):
    served, key, baseline = world
    for op in ("ticket.query", "needs"):  # the owner sees the restricted ticket's row; no-person's answer has no trace
        c = served.client()
        res = c.req(op, args_for(op, key), session=SESSION)
        assert res["ok"] and TITLE not in json.dumps(res) and key not in json.dumps(res), op
    assert TITLE in baseline["ticket.query"]  # the precondition: the read does show the ticket to someone


def test_list_and_search_count_only_visible_rows(world):
    served, key, _ = world
    c = served.client()
    listed = ok(c.req("list"))
    assert listed["data"]["count"] == 1 and key not in json.dumps(listed)
    assert key not in json.dumps(ok(c.req("search", {"query": TITLE})))
