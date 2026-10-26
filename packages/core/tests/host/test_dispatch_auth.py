"""H4a1: principals, the grant secret refused in args, the reads rule, F4 and the F6 visibility of the served reads,
and the property that no response carries key material."""

from __future__ import annotations

import base64
import errno
import json
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import orch.ops as ops
from orch import canon, crypto
from orch.host.auth import GRANT_SHAPE, Redactor
from orch.host.dispatch import served_ops
from tests.host.dispatch_helpers import SESSION, Served, err, ok
from tests.host.lifecycle_helpers import Lab
from tests.ops.helpers import OTHER
from tests.store.helpers import GRANT_SECRET


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
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
    return f"{grant.partition('.')[0]}.{crypto.b64u(b'w' * 32)}"


def tickets_on_disk(served: Served) -> list[str]:
    path = os.path.join(served.lab.root, ".orch", "tickets")
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


# --- the grant secret in args ---------------------------------------------------------------------------------------


def test_a_grant_anywhere_in_args_opts_or_files_is_refused_and_nothing_runs(served):
    c, g = served.client(), served.grant
    secret = g.partition(".")[2]
    uid, key = served.ticket()
    before = served.lab.ws.read_events(uid)
    cases = [
        ("log", {"text": f"see {g}", "ref": key}, {}),
        ("log", {"text": f"only the secret {secret}", "ref": key}, {"grant": g}),
        ("log", {"text": "x", "ref": key, "message": "gr_01J9ZP0000000000000000000G." + "a" * 43}, {}),
        ("new", {"title": "t", "label": ["ok", g]}, {"grant": g}),
        ("log", {"file": "notes.txt", "ref": key}, {"files": {"file": {"b64": base64.b64encode(g.encode()).decode()}}}),
        ("status", {}, {"files": {"x": {"b64": base64.b64encode(f"..{secret}..".encode()).decode()}}, "grant": g}),
    ]
    for op, args, extra in cases:
        res = c.req(op, args, session=SESSION, idem=True, **extra)
        assert err(res) == "grant.secret_in_args", (op, args)
        assert secret not in json.dumps(res)
    assert served.lab.ws.read_events(uid) == before


def test_a_grant_in_opts_idem_is_refused_and_never_echoed(served):
    """``opts.idem`` has its own pattern, so a grant there fails the envelope (step 2) before the secret check."""
    res = served.client().ws.call("status", {}, "r", opts={"idem": served.grant})
    assert err(res) == "req.invalid"
    assert served.grant.partition(".")[2] not in json.dumps(res) and not GRANT_SHAPE.search(json.dumps(res))


# --- the reads rule (F1 §10.7) --------------------------------------------------------------------------------------


def test_an_invalid_grant_on_a_read_gives_workspace_tickets_only(served):
    owner = served.lab.owner
    open_uid, open_key = served.ticket("open to all")
    secret_uid, secret_key = served.ticket("restricted")
    served.restrict(secret_uid, [owner])
    c = served.client()
    good = ok(c.req("list", grant=served.grant))
    assert good["data"]["count"] == 2
    assert ok(c.req("show", {"ref": secret_key}, grant=served.grant))["key"] == secret_key
    missing = c.req("show", {"ref": "DEMO-9999"})
    for bad in (wrong_secret(served.grant), "gr_malformed", None):
        listed = ok(c.req("list", grant=bad, session=SESSION))
        if bad is None:  # column P: the mapped owner is the viewer
            assert listed["data"]["count"] == 2
            continue
        assert [t["key"] for t in listed["data"]["tickets"]] == [open_key]
        assert listed["data"]["count"] == 1
        hidden = c.req("show", {"ref": secret_key}, grant=bad)
        assert err(hidden) == err(missing) == "not_found"
        same = missing["error"]["message"].replace("DEMO-9999", "K")
        assert hidden["error"]["message"].replace(secret_key, "K") == same
        assert set(hidden["error"]) == set(missing["error"])
        assert "restricted" not in json.dumps(ok(c.req("search", {"query": "restricted"}, grant=bad)))
        assert err(c.req("artifact.list", {"ref": secret_key}, grant=bad)) == "not_found"
        assert err(c.req("task.list", {"ref": secret_key}, grant=bad)) == "not_found"


def test_a_person_sees_restricted_tickets_by_its_mapping_and_an_agent_by_its_grant(served, lab):
    quinn = lab.add_member()
    uid, key = served.ticket("quinn only")
    served.restrict(uid, [quinn.ref])
    c = served.client()
    # the owner (column P) and the owner's grant (G) are not on the list
    assert err(c.req("show", {"ref": key})) == "not_found"
    assert err(c.req("show", {"ref": key}, grant=served.grant)) == "not_found"
    assert ok(c.req("list"))["data"]["count"] == 0


def test_a_grant_of_another_workspace_is_refused_here(served):
    """An agent that points ORCH_WORKSPACE at a workspace other than the one that started it carries a grant this
    workspace never issued: a write is refused and a read sees only what is open to all."""
    c = served.client()
    foreign = "gr_01J9ZP00000000000000000000." + crypto.b64u(b"f" * 32)
    assert err(c.req("log", {"text": "a note"}, grant=foreign, session=SESSION, idem=True)) == "grant.expired"
    assert tickets_on_disk(served) == []


# --- F4: principals ---------------------------------------------------------------------------------------------------


def test_another_persons_grant_id_with_a_wrong_secret_reaches_no_outcome_and_no_notes(served):
    c, g = served.client(), served.grant
    idem = "a_01J9ZP0000000000000000000C"
    made = ok(c.req("new", {"title": "mine"}, grant=g, session=SESSION, idem=idem))
    key = made["key"]
    assert ok(c.req("claim", {"ref": key}, grant=g, session=SESSION, idem=True))
    assert ok(c.req("outcome.get", {"idem": idem}, grant=g))["data"]["state"] == "committed"
    bad = wrong_secret(g)
    assert err(c.req("outcome.get", {"idem": idem}, grant=bad)) == "outcome.unknown_idem"
    assert err(c.req("outcome.get", {"idem": idem})) == "outcome.unknown_idem"  # uid:<n> is another principal too
    # notes: the claim is in the grant principal's notes; the same session label from another principal sees none
    mine = ok(c.req("status", grant=g, session=SESSION))["data"]
    assert mine.get("claim") == key
    other = c.req("log", {"text": "a note"}, grant=bad, session=SESSION, idem=True)
    assert err(other) == "grant.expired"  # an unverified grant on a write
    anon = c.req("log", {"text": "a note"}, session=SESSION, idem=True)
    assert err(anon) == "ambiguous_ref" and key not in json.dumps(anon)  # "your claims: none"
    assert ok(c.req("status", session=SESSION))["data"].get("claim") is None
    # the same retry idem from another principal is a separate record: it runs (and is refused) on its own
    again = c.req("new", {"title": "mine"}, session=SESSION, idem=idem)
    assert err(again) == "op.not_allowed"


def test_the_principal_never_comes_from_the_session_label(served):
    c, g = served.client(), served.grant
    assert ok(c.req("whoami", session=OTHER))["data"]["grant"] is None
    assert ok(c.req("whoami", grant=g, session=OTHER))["data"]["grant"]["id"] == served.lab.ws.grant_id


# --- redaction --------------------------------------------------------------------------------------------------------


def test_results_are_redacted_before_they_are_stored_or_sent(served):
    """A handler that echoes the grant and key material: the answer and the stored outcome record carry none of it."""
    from orch.ops import Result

    g = served.grant
    secret = g.partition(".")[2]
    original = ops.get("log")

    def leaky(ctx, args):
        return Result(
            data={"echo": ctx.grant, "secret_hash": "sha256:" + "0" * 64, "nested": [{"seed": "x", "ok": secret}]},
            lines=["-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"],
        )

    ops.bind("log", leaky)
    try:
        idem = "a_01J9ZP0000000000000000000D"
        res = served.client().req("log", {"text": "x"}, grant=g, session=SESSION, idem=idem)
    finally:
        ops.bind("log", original.handler)
    body = json.dumps(ok(res))
    assert secret not in body and g not in body and "secret_hash" not in body and "PRIVATE KEY" not in body
    assert '"seed"' not in body
    records = served.lab.state / "hosts" / served.wid / "outcomes"
    stored = "".join(p.read_text() for p in records.iterdir())
    assert stored and secret not in stored and "secret_hash" not in stored and "PRIVATE KEY" not in stored


def _key_material(served: Served) -> list[str]:
    """Strings that must never appear in a response: the grant, its secret and hash, the WSK private key files."""
    g = served.grant
    out = [g, g.partition(".")[2], canon.grant_secret_hash(GRANT_SECRET), crypto.b64u(GRANT_SECRET)]
    keys = served.lab.state / "hosts" / served.wid / "keys"
    for path in keys.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for chunk in (raw[i : i + 24] for i in range(0, len(raw), 24)):
                if len(chunk) == 24:
                    out += [base64.b64encode(chunk).decode()[:24], chunk.hex()]
    return [s for s in out if s]


_OPS = sorted(served_ops())
_VALUE = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-5, 5),
        st.sampled_from(["DEMO-0001", "DEMO-0002", "1", "x", "-", "../x", "T1", "AC1", "agent", SESSION]),
        st.text(max_size=12),
    ),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(["a", "b", "ref"]), inner, max_size=2),
    max_leaves=6,
)
_ARGS = st.dictionaries(
    st.sampled_from(["ref", "title", "text", "message", "q", "op", "status", "limit", "type", "label", "name", "file"]),
    _VALUE,
    max_size=3,
)


@pytest.mark.slow
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    op=st.sampled_from(_OPS),
    args=_ARGS,
    auth=st.sampled_from(["grant", "wrong", "none", "malformed"]),
    session=st.sampled_from([None, SESSION]),
    idem=st.booleans(),
    leak=st.sampled_from([None, "grant", "secret", "shape"]),
)
def test_no_response_ever_contains_key_material(served_once, op, args, auth, session, idem, leak):
    served = served_once
    g = served.grant
    grant = {"grant": g, "wrong": wrong_secret(g), "none": None, "malformed": "gr_x.y"}[auth]
    if leak is not None:
        args = {**args, "text": {"grant": g, "secret": g.partition(".")[2], "shape": wrong_secret(g)}[leak]}
    res = served.client_once.req(op, args, grant=grant, session=session, idem=idem or None)
    body = json.dumps(res)
    for material in served.material:
        assert material not in body, (op, args, material[:6])
    assert not GRANT_SHAPE.search(body)
    assert "PRIVATE KEY" not in body


@pytest.fixture
def served_once(served):
    served.ticket("one")
    uid, _ = served.ticket("two")
    served.restrict(uid, [served.lab.owner])
    served.material = _key_material(served)
    served.client_once = served.client()
    return served


@given(
    st.recursive(
        st.one_of(st.text(), st.integers(), st.sampled_from(["SECRET", "gr_01J9ZP0000000000000000000G." + "q" * 43])),
        lambda inner: (
            st.lists(inner, max_size=4)
            | st.dictionaries(st.one_of(st.text(max_size=5), st.sampled_from(["secret_hash", "seed", "SECRET"])), inner)
        ),
        max_leaves=20,
    )
)
def test_the_redactor_never_lets_a_secret_or_a_grant_through(value):
    grant = "gr_01J9ZP0000000000000000000H." + "z" * 43
    r = Redactor(grant)
    out = json.dumps(r.obj({"v": value, "g": grant, "s": "z" * 43}))
    assert "z" * 43 not in out and not GRANT_SHAPE.search(out) and '"secret_hash"' not in out and '"seed"' not in out
