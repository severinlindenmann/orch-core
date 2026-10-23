"""H4a1: the closed op list (registry × column of §0.5), the §0.6 precedence, ``whoami``, ``host.status`` and the
error catalog with its dispositions."""

from __future__ import annotations

import dataclasses
import errno
import re
from pathlib import Path

import pytest

import orch.ops as ops
from orch.host import frames
from orch.host.agents import AGENT_OPS
from orch.host.columns import Column, refusal, resolve
from orch.host.dispatch import FILE_OPS, GIT_OPS, HOST_OPS, NOT_SERVED, PERSON_READS, op_kind, served_ops
from orch.host.errors import HostError, disposition
from orch.host.hubops import HUB_OPS
from orch.host.human import HUMAN_OPS
from orch.host.peerops import PEER_OPS, PEER_READS
from orch.host.publishing import PUBLISH_OPS
from orch.host.reads import READ_OPS
from orch.host.records import RECORDS_OPS
from orch.host.review import HOST_PERSON_OPS
from orch.host.runnergrants import GRANT_OPS
from orch.host.runners import RUN_OPS
from orch.host.shareops import SHARE_OPS, SHARE_READS
from orch.host.spaceops import SPACE_OPS, SPACE_READS
from orch.host.terms import TERM_OPS
from orch.host.worktrees import WORKTREE_OPS
from orch.ops.errors import ERRORS
from tests.host.dispatch_helpers import SESSION, UID, Served, err, ok
from tests.host.lifecycle_helpers import REPO, Lab

BAD_SECRET = "gr_01J9ZP0000000000000000000G." + "A" * 43


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


@pytest.fixture
def served(lab):
    s = Served(lab)
    try:
        yield s
    finally:
        s.close()


def wrong_secret(grant: str) -> str:
    """The same grant id with another well-formed secret."""
    from orch import crypto

    return f"{grant.partition('.')[0]}.{crypto.b64u(b'w' * 32)}"


def as_agent_class(served: Served) -> None:
    """Make the test UID an agent-class peer of the workspace (the host UID cannot be registered as one)."""
    registry = served.h.host.registry
    entry = registry.get(served.wid)
    with registry._lock:
        registry._entries[served.wid] = dataclasses.replace(entry, peers={str(UID): {"class": "agent"}})


# --- the closed list ------------------------------------------------------------------------------------------------


def test_welcome_lists_exactly_the_registry_ops_and_the_host_ops(served):
    welcome = served.client().welcome
    registry = {op.name for op in ops.all()}
    host = set(HOST_OPS) | set(FILE_OPS) | set(GIT_OPS) | set(HUMAN_OPS) | set(READ_OPS) | set(HOST_PERSON_OPS)
    host |= set(RECORDS_OPS)
    host |= set(RUN_OPS) | set(TERM_OPS)  # H11a, H11b
    host |= set(WORKTREE_OPS)  # H11d
    host |= set(PUBLISH_OPS)  # P5
    host |= set(HUB_OPS)  # V3 C2a
    host |= set(PEER_OPS)  # V3 C12
    host |= set(SPACE_OPS)  # V3 C13
    host |= set(SHARE_OPS)  # V3 C6
    host |= set(GRANT_OPS) | set(AGENT_OPS)  # H11c
    host |= {"reader.declare"}  # H12d-2
    host |= {"phone.request", "phone.cancel"}  # B10 Part B
    host |= {"workdir.get", "workdir.delete"}  # r3 part C
    assert set(welcome["ops"]) == (registry - set(NOT_SERVED)) | host
    assert set(READ_OPS) == {  # H6b
        "ticket.query", "ticket.get", "needs", "activity.list", "workspace.show", "member.list", "session.list",
        "grant.list", "workspace.settings", "ticket.text", "reader.list", "phone.requests", "phone.status",
        "gate.input",
    }  # fmt: skip
    assert set(HOST_PERSON_OPS) == {
        "move",
        "policy.set",
        "settings.set",
        "people.set",
        "relay.pair_device",
        "publish.share",
        "ac.accept",
        "ac.unaccept",
        "task.mark_done",
        "hub.join",
        "hub.mode",
        "hub.leave",
    }  # H12c-e, B12D, P5, R3, V3 C2a, C2b
    assert set(HUMAN_OPS) == {"human.prepare", "human.get", "human.submit", "human.cancel", "human.pending"}
    assert set(NOT_SERVED) == {"init", "import.v1"}
    assert {"wait", "sub.open", "sub.close"} <= set(welcome["ops"])  # H6a: served on the bus
    # §0.4: every op at version 1 except task.done at 2 (H3b2's contract, listed since H7)
    assert {k: v for k, v in welcome["ops"].items() if v != 1} == {"task.done": 2}
    assert {"ops/1", "outcome/1", "sub/1", "blob/1", "diff/1"} <= set(welcome["caps"])
    assert set(FILE_OPS) == {"blob.put", "blob.drop", "artifact.get"}
    assert GIT_OPS == {"diff": 1} and welcome["ops"]["diff"] == 1  # diff/1 lists both diff and artifact.get
    # H6b: the read caps; each read_ext/N also lists the lower ones (§0.4; read_ext/3: v1.9 member.list devices)
    caps = {"query/1", "read_ext/1", "read_ext/2", "read_ext/3", "activity/1", "settings/1", "text/1"}
    assert caps <= set(welcome["caps"])
    assert welcome["peer"] == {"uid": UID, "class": "person", "person": served.lab.owner}


@pytest.mark.parametrize("op", ["init", "nope", "host.list", "job.next", "human.sign"])
def test_an_op_outside_the_list_is_unknown_command(served, op):
    assert err(served.client().req(op)) == "unknown_command"


def test_import_v1_cannot_even_be_named_on_the_wire(served):
    assert err(served.client().req("import.v1")) == "req.invalid"  # OpName has no digits (§0.3)


def test_a_different_contract_version_is_op_unsupported_and_other_ops_keep_working(served):
    c = served.client(hello_ops={"status": 2})
    assert err(c.req("status")) == "op.unsupported"
    assert ok(c.req("whoami"))["data"]["person"] == served.lab.owner
    # the op check (step 3) comes before the secret check (step 5)
    assert err(c.req("status", {"x": BAD_SECRET})) == "op.unsupported"


# The §0.5 matrix restated from the design text, independently of orch.host.columns.
ROWS = {
    # name: (peer class, auth.grant state, person mapping valid)
    "P": ("person", "absent", True),
    "G person": ("person", "verified", True),
    "N removed person": ("person", "absent", False),
    "N person, unverified grant": ("person", "unverified", True),
    "N person, malformed grant": ("person", "malformed", True),
    "N agent": ("agent", "absent", False),
    "G agent": ("agent", "verified", False),
    "N agent, unverified grant": ("agent", "unverified", False),
}
ALLOWED = {
    "P": {"read", "person_read", "pg_read", "person_write", "pg_write", "unattended"},
    # deposit (H11c): column G at the matrix; the op itself requires the person class and the UID's mapping
    "G": {"read", "pg_read", "pg_write", "agent", "unattended", "deposit"},
    "N": {"read", "unattended"},
}


def expected(row: str, kind: str) -> str | None:
    if kind == "human":
        return "human_only"  # a human registry op as a plain req, in every workspace column
    if kind == "human_api":  # human.*: column P only; with a grant (verified or not) from person class, human_only
        if row == "P":
            return None
        return "human_only" if row.startswith(("G person", "N person")) else "op.not_allowed"
    write = kind in {"agent", "unattended", "pg_write", "deposit"}
    if write and "unverified" in row:
        return "grant.expired"
    if write and "malformed" in row:
        return "grant.required"
    if write and row == "N removed person":
        return "op.not_allowed"  # L1: a removed member's UID keeps no unattended writes
    column = row.split()[0]
    return None if kind in ALLOWED[column] else "op.not_allowed"


def test_every_served_op_in_every_row_of_the_effective_column_table():
    seen = set()
    for op in served_ops():
        kind = op_kind(op)
        assert kind is not None
        if op in HOST_OPS or op in FILE_OPS or op in GIT_OPS:
            assert kind == ("read", "read")
        elif op in READ_OPS:
            assert kind == ("pg_read" if op in {"session.list", "grant.list"} else "read", "read")
        elif op in HOST_PERSON_OPS:
            assert kind == ("human", "write")
        elif op in HUMAN_OPS:
            assert kind == ("human_api", "write" if op == "human.submit" else "read")
        elif op == "reader.declare":  # H12d-2: P only on a socket (column R: H12d-3)
            assert kind == ("person_write", "write")
        elif op in {"phone.request", "phone.cancel"}:  # B10 Part B: P only
            assert kind == ("person_write", "write")
        elif op in {"workdir.get", "workdir.delete"}:  # r3 part C: P only
            assert kind == (("person_read", "read") if op == "workdir.get" else ("person_write", "write"))
        elif op in RECORDS_OPS:
            write = op in {"records.remote_private", "records.note"}
            assert kind == (("person_write", "write") if write else ("person_read", "read"))
        elif op == "term.preview":  # H11b: P only
            assert kind == ("person_read", "read")
        elif op in TERM_OPS or op == "run.stop":  # H11b: P only
            assert kind == ("person_write", "write")
        elif op in RUN_OPS:  # H11a: P and G only
            assert kind == ("pg_read", "read")
        elif op in WORKTREE_OPS:  # H11d: P and G (never N); list reads, create and remove write
            assert kind == (("pg_read", "read") if op == "worktree.list" else ("pg_write", "write"))
        elif op in PUBLISH_OPS:  # P5: prepare and discard P and G, list reads, send/revoke/register P only
            want = {
                "publish.prepare": ("pg_write", "write"),
                "publish.discard": ("pg_write", "write"),
                "publish.list": ("pg_read", "read"),
            }.get(op, ("person_write", "write"))
            assert kind == want
        elif op in HUB_OPS:  # V3 C2a: P only (the op checks the owner role); status reads
            assert kind == (
                ("person_read", "read") if op in ("hub.status", "hub.handoff") else ("person_write", "write")
            )
        elif op in SHARE_OPS:  # V3 C6: column P only (owner role)
            assert kind == (("person_read", "read") if op in SHARE_READS else ("person_write", "write"))
        elif op in SPACE_OPS:  # V3 C13: list and get for P and G, the rest P only (owner role)
            assert kind == (("pg_read", "read") if op in SPACE_READS else ("person_write", "write"))
        elif op in PEER_OPS:  # V3 C12: reads for P and G, writes P only (owner role)
            assert kind == (("pg_read", "read") if op in PEER_READS else ("person_write", "write"))
        elif op in AGENT_OPS:  # H11c: P only
            assert kind == (("person_read", "read") if op == "agent.preview" else ("person_write", "write"))
        elif op in GRANT_OPS:  # H11c: deposit needs a grant; status and forget are P only
            assert kind == {"grant.deposit": ("deposit", "write"), "grant.deposit_status": ("person_read", "read"),
                            "grant.forget": ("person_write", "write")}[op]  # fmt: skip
        else:
            who = ops.get(op).who
            assert kind[0] == ("person_read" if op in PERSON_READS else who)
            assert kind[1] == ("wait" if op == "wait" else "read" if who == "read" else "write")
        for row, (peer, grant, valid) in ROWS.items():
            assert refusal(resolve(peer, grant, valid), kind[0]) == expected(row, kind[0]), (op, row)
            seen.add((kind[0], expected(row, kind[0])))
    kinds = {
        "read",
        "person_read",
        "pg_read",
        "person_write",
        "pg_write",
        "agent",
        "unattended",
        "human",
        "human_api",
        "deposit",
    }
    assert kinds == {k for k, _ in seen}
    for op in NOT_SERVED:
        assert op_kind(op) is None


# One op of each category, end to end, per row (the snapshot path and the worker-thread path agree).
SAMPLES = {
    "status": {},
    "doctor": {},
    "new": {"title": "x"},
    "log": {"text": "a note", "ref": "DEMO-0001"},
    "approve": {"ref": "DEMO-0001"},
    "human.cancel": {"request_id": "hr_" + "0" * 26},
}


def _check_row(served, row, grant):
    c = served.client()
    for op, args in SAMPLES.items():
        want = expected(row, op_kind(op)[0])
        res = c.req(op, args, grant=grant, session=SESSION, idem=True)
        if want is None:
            if not res["ok"]:  # allowed by the column: whatever the op itself answers, never a column refusal
                assert res["error"]["code"] not in {"op.not_allowed", "human_only", "grant.required", "grant.expired"}
        else:
            assert err(res) == want, (row, op)


def test_rows_end_to_end_for_a_person_class_uid(served):
    served.ticket()
    _check_row(served, "P", None)
    _check_row(served, "G person", served.grant)
    _check_row(served, "N person, unverified grant", wrong_secret(served.grant))
    _check_row(served, "N person, malformed grant", "gr_not-a-grant")


def test_rows_end_to_end_for_an_agent_class_uid(served):
    served.ticket()
    as_agent_class(served)
    assert served.client().welcome["peer"]["class"] == "agent"
    _check_row(served, "N agent", None)
    _check_row(served, "G agent", served.grant)
    _check_row(served, "N agent, unverified grant", wrong_secret(served.grant))


def test_a_removed_members_uid_is_column_n(lab):
    quinn = lab.add_member()
    s = Served.__new__(Served)
    s.lab = lab
    s.h = lab.host(clock=lambda: lab.ws.clock[0])
    s.h.__enter__()
    s.control = s.h.client()
    s.wid = ok(lab.register(s.control, peers=lab.peers(quinn.ref)))["data"]["workspace_id"]
    s.service = s.h.host.services[s.wid]
    s.clients = []
    try:
        s.ticket()
        assert ok(s.client().req("whoami"))["data"]["person"] == quinn.ref
        lab.remove_member(quinn)
        _check_row(s, "N removed person", None)
        assert ok(s.client().req("whoami"))["data"]["person"] is None
    finally:
        s.close()


# --- precedence (§0.6) ----------------------------------------------------------------------------------------------


def test_the_column_comes_before_the_rate_limit(served):
    """An agent's requests outside its column answer op.not_allowed and never use a rate token (step 6 before 7)."""
    from orch.host.transport import Bucket

    as_agent_class(served)
    c = served.client()
    served.h.host.limits.agents[UID] = Bucket(0, 5)  # no refill, a burst of 5
    for _ in range(20):
        assert err(c.req("doctor")) == "op.not_allowed"
        assert err(c.req("new", {"title": "x"}, grant=wrong_secret(served.grant), idem=True)) == "grant.expired"
    for _ in range(5):  # the bucket is still full: refusals of steps 5 and 6 took no token
        assert ok(c.req("whoami"))["data"]["peer"]["class"] == "agent"
    assert err(c.req("whoami")) == "rate.limited"
    assert err(c.req("doctor")) == "op.not_allowed"  # still the column's answer, not the rate's


def test_the_secret_check_comes_before_the_column_and_after_the_op(served):
    c = served.client()
    assert err(c.req("approve", {"ref": BAD_SECRET})) == "grant.secret_in_args"  # not human_only
    assert err(c.req("nope.op", {"ref": BAD_SECRET})) == "unknown_command"


def test_an_unverified_grant_on_a_write_beats_the_column(served):
    c = served.client()
    # new is an agent op, outside column N: an unverified grant still answers grant.expired (§0.6 step 5)
    assert err(c.req("new", {"title": "x"}, grant=wrong_secret(served.grant), idem=True)) == "grant.expired"
    assert err(c.req("doctor", grant=wrong_secret(served.grant))) == "op.not_allowed"  # not a write


def test_schema_then_files_then_dry_run_then_idem(served):
    c, g = served.client(), served.grant
    assert err(c.req("new", {"title": "x", "bogus": 1}, grant=g, session=SESSION)) == "invalid.input"
    files = {"nofile": {"b64": "aGk="}}
    assert err(c.req("new", {"title": "x"}, grant=g, session=SESSION, files=files)) == "blob.invalid"
    assert err(c.req("status", dry_run=True)) == "invalid.input"
    assert err(c.req("new", {"title": "x"}, grant=g, session=SESSION)) == "idem.required"
    res = ok(c.req("new", {"title": "x"}, grant=g, session=SESSION, dry_run=True))
    assert res["seq"] == 1 and "dry-run: nothing was written" in res["lines"]


def test_a_replay_comes_before_the_workspace_state(served):
    c, g = served.client(), served.grant
    first = ok(c.req("new", {"title": "x"}, grant=g, session=SESSION, idem="a_01J9ZP0000000000000000000A"))
    service = served.service
    service._judge = lambda: setattr(service, "state", "degraded") or setattr(service, "cause", "test")
    again = ok(c.req("new", {"title": "x"}, grant=g, session=SESSION, idem="a_01J9ZP0000000000000000000A"))
    assert again["duplicate"] is True and again["key"] == first["key"]
    fresh = c.req("new", {"title": "x"}, grant=g, session=SESSION, idem="a_01J9ZP0000000000000000000B")
    assert err(fresh) == "workspace.degraded"
    assert ok(c.req("list"))["data"]["count"] == 1  # reads work
    assert ok(c.req("whoami"))


# --- whoami and host.status (§0.8) ----------------------------------------------------------------------------------


def test_whoami_per_column(served):
    c = served.client()
    p = ok(c.req("whoami", session=SESSION))["data"]
    assert p == {
        "peer": {"uid": UID, "class": "person"},
        "person": served.lab.owner,
        "tier": "A",
        "grant": None,
        "session": SESSION,
        "runner": {"connected": False, "enabled": False, "terminals": False, "gen": None, "grant": None},
    }
    g = ok(c.req("whoami", grant=served.grant))["data"]
    assert g["person"] == served.lab.owner and g["grant"]["id"] == served.lab.ws.grant_id
    assert g["grant"]["verbs"] == "agent" and g["grant"]["scope"] == "all" and g["grant"]["until"].endswith("Z")
    assert g["session"] is None
    bad = ok(c.req("whoami", grant=wrong_secret(served.grant)))["data"]
    assert bad["grant"] is None and bad["person"] is None  # an unverified grant is no identity
    assert err(c.req("whoami", {"x": 1})) == "invalid.input"
    assert err(c.req("whoami", dry_run=True)) == "invalid.input"


def test_host_status_gives_column_n_only_version_and_tier(served):
    c = served.client()
    p = ok(c.req("host.status"))["data"]
    assert set(p) == {"version", "tier", "state", "uptime_s"} and p["state"] == "ok"
    assert set(ok(c.req("host.status", grant=served.grant))["data"]) == {"version", "tier", "state", "uptime_s"}
    n = ok(c.req("host.status", grant=wrong_secret(served.grant)))["data"]
    assert set(n) == {"version", "tier"}


# --- the error catalog (§0.6) ---------------------------------------------------------------------------------------


def _catalog_rows() -> list[tuple[str, int, bool, str]]:
    text = (REPO / "docs" / "architecture" / "orch-v2-host.md").read_text()
    lines = text.split("### 0.6 Errors and their disposition", 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Code |"))
    table = []
    for line in lines[start + 2 :]:  # the header and its rule
        if not line.startswith("|"):
            break  # the table ends at its first non-table line
        table.append(line)
    rows = []
    for line in table:
        m = re.match(r"\| `([a-z_.]+)` \| (\d) \| (yes|no) \| (\w+)", line)
        if m:
            rows.append((m.group(1), int(m.group(2)), m.group(3) == "yes", m.group(4)))
    assert len(rows) == len(table), [line for line in table if not re.match(r"\| `[a-z_.]+` \| \d", line)]
    # H11d: the worktree.* rows sit in a later table of the section, after a prose paragraph
    rest = "\n".join(lines[start + 2 + len(table) :]).split("\n### ", 1)[0]
    for line in rest.splitlines():
        m = re.match(r"\| `((?:worktree\.|publish\.|unsupported\.|hub\.)[a-z_]+)` \| (\d) \| (yes|no) \| (\w+)", line)
        if m and m.group(1) not in {r[0] for r in rows}:
            rows.append((m.group(1), int(m.group(2)), m.group(3) == "yes", m.group(4)))
    return rows


def test_every_host_code_has_the_tables_exit_and_retryability():
    from orch.host.errors import HOST_CODES

    rows = _catalog_rows()
    assert len(rows) >= 35
    # the other way: every code the host table holds is in §0.6's table, or a P1 code §0.6 keeps as it is
    p1_kept = {"store.busy", "custody.unsafe_mode", "trust.genesis_mismatch", "unknown_command", "invalid.input"}
    p1_kept |= {"not_found", "internal", "human_only", "grant.required", "grant.expired"}
    for code in HOST_CODES:
        assert code in {r[0] for r in rows} or code in p1_kept, code
    for code, exit_code, retryable, fate in rows:
        assert disposition(code) == (exit_code, retryable), code
        e = HostError(code)
        assert (e.exit, e.retryable) == (exit_code, retryable), code
        if code in frames._DISPOSITIONS:
            assert frames._DISPOSITIONS[code][:2] == (exit_code, retryable), code
            assert frames._DISPOSITIONS[code][2] == fate, code
    # P1 codes keep their catalog exit and retryability on the socket (lock.busy is the documented exception)
    for code, spec in ERRORS.items():
        if code not in {r[0] for r in rows}:
            assert disposition(code) == (spec.exit, spec.retryable), code


def test_a_refusal_carries_the_catalogs_hint_and_fix_and_its_exit(served):
    c = served.client()
    res = c.req("show", {"ref": "DEMO-9999"})
    assert err(res) == "not_found" and res["exit"] == ERRORS["not_found"].exit
    assert res["error"]["retryable"] is False
    assert "hint" in res["error"] or "fix" in res["error"]
    frames.validate_envelope(res, sender="host")


def test_errors_and_results_are_valid_frames(served):
    c = served.client()
    reads = [("list", {}), ("status", {}), ("help", {}), ("describe", {"op": "new"}), ("search", {"query": "x"})]
    for op, args in reads:
        res = c.req(op, args)
        frames.validate_envelope(res, sender="host")


def test_the_design_doc_is_where_the_catalog_test_reads_it():
    assert Path(REPO / "docs" / "architecture" / "orch-v2-host.md").is_file()


def test_grant_deposit_is_a_person_with_their_own_verified_grant():
    """D-P2-27 as amended (#466 review, owner decision): at the matrix ``grant.deposit`` needs a verified
    ``auth.grant`` (column G); the op itself then requires the person class, the UID's valid mapping to a member and
    that the grant is that person's own runner grant (tests/host/test_runner_grants.py). Without a grant (P), with an
    unverified or malformed one, and in every non-workspace column it is refused at the matrix."""
    kind = op_kind("grant.deposit")
    assert kind == ("deposit", "write")
    for row, (peer, grant, valid) in ROWS.items():
        got = refusal(resolve(peer, grant, valid), kind[0])
        if grant == "verified":
            assert got is None, row  # person or agent class: the op decides (an agent class peer is refused there)
        else:
            assert got is not None, row
    for name in ("W", "C"):
        assert refusal(Column(name, "none"), "deposit") == "op.not_allowed"
