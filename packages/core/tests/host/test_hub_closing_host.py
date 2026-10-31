"""#1463 (hub protocol 9.7): what a host does with a ``hub.closing`` notice. A served workspace joined to the TLS test
hub, which answers ``closing_get`` and may push ``closing_notice``. The notice is verified with the held card, the
host's own workspace and CURRENT binding; a verified one stops company content and shows core's own banner status; it
never deletes a file, never leaves, and nothing happens at ``ends_ms``."""

from __future__ import annotations

import json
import os
import time

import pytest

from orch import crypto
from orch.crypto import suite
from orch.host import hubclosing, hubops, orgsecrets, orgstate, peerops, securitylog
from orch.hubclient import orgbundles as OB
from orch.hubclient import wire
from orch.hubclient.tlslink import framing
from tests.host import human_helpers
from tests.host import test_org_ops as O
from tests.host.dispatch_helpers import ok
from tests.host.test_hub_join_real import now_ms
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.host.test_org_ops import TEXT, req, svc, sync

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
org, dev_workspace, no_log_sync_sender = O.org, O.dev_workspace, O.no_log_sync_sender  # the fixtures of the org tests

DAY = 86_400_000


def notice(org, served, *, key=None, binding_seq=1, workspace=None, issued=None, ends=None, **over):
    """A ``hub.closing`` as the hub signs it (HK_op of the test hub unless ``key``)."""
    now = now_ms(served)
    o = {
        "v": 2, "suite": 2, "kind": "hub.closing", "hv": 0, "hub_id": org.hub.hub_id, "op_cert_id": org.op_cert_id,
        "workspace_id": workspace or served.wid, "binding_seq": binding_seq, "removal_id": os.urandom(16).hex(),
        "issued_ms": now - 1000 if issued is None else issued, "ends_ms": now + 3 * DAY if ends is None else ends,
        "dev": True,
    }  # fmt: skip
    o.update(over)
    return {"o": o, "sig": crypto.b64u(suite.sign(key or org.hub.hk_op, wire.SIG_CLOSING + wire.cj(o)))}


class Closing:
    """What the hub answers to ``closing_get`` (``reply``) and records of the host's acks."""

    def __init__(self, org) -> None:
        self.reply: object = {"closing": None}
        self.acks: list[dict] = []
        self.push_next: dict | None = None
        self.org = org
        inner = org.handle

        def handle(h, o):
            if o["op"] == "closing_get":
                if self.push_next is not None:
                    push(h, self.push_next, o["ts_ms"])
                    self.push_next = None
                return h.reply(o, body=self.reply, ts_ms=o["ts_ms"])
            if o["op"] == "closing_notice":
                self.acks.append(o)
                return b""
            return inner(h, o)

        org.handle = handle


def push(hub, signed, ts_ms) -> None:
    o = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
        "dir": "h2w", "seq": hub.next_seq(), "ts_ms": ts_ms, "op": "closing_notice", "re": None,
        "body": {"notice": signed},
    }  # fmt: skip
    framing.write_frame(hub._conns[-1], hub.sign_frame(o), timeout=5)  # noqa: SLF001


@pytest.fixture
def cl(org):
    return Closing(org)


@pytest.fixture
def accepted(served, org):
    """One company skill, offered and accepted: served until the notice."""
    org.skills = [org.skill("invoices")]
    sync(served)
    ok(req(served, "org.accept", {"kind": "skill", "name": "invoices", "digest": OB.skill_digest("invoices", TEXT)}))
    assert [s["name"] for s in orgstate.served(svc(served))["skills"]] == ["invoices"]


def status(served) -> dict:
    return ok(req(served, "hub.status"))["data"]


def audit_codes(served) -> list[str]:
    path = svc(served).state_dir / "host" / securitylog.NAME
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    return [r["code"] for r in rows if r["event"] == "hub.closing_refused"]


def files(served) -> dict[str, bytes]:
    d = orgstate.org_dir(svc(served))
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def test_no_notice_means_no_closing(served, org, cl, accepted):
    sync(served)
    assert status(served)["closing"] is None
    assert [s["name"] for s in orgstate.served(svc(served))["skills"]] == ["invoices"]


def test_a_verified_notice_sets_the_status_stops_serving_drops_the_cache_and_keeps_the_files(
    served, org, cl, accepted, monkeypatch
):
    dropped: list[str] = []
    real = orgsecrets.drop_cache
    monkeypatch.setattr(orgsecrets, "drop_cache", lambda s: (dropped.append("x"), real(s))[1])
    before = files(served)
    n = notice(org, served)
    cl.reply = {"closing": n}
    sync(served)
    st = status(served)["closing"]
    assert st == {"since_ms": n["o"]["issued_ms"], "ends_ms": n["o"]["ends_ms"]}  # only these two members leave core
    assert orgstate.served(svc(served)) == {"skills": [], "mcp": []}
    assert dropped  # the opened company secrets are forgotten
    assert files(served) == before or set(before) <= set(files(served))  # nothing deleted
    for name, data in before.items():
        if name != "offers.json":
            assert files(served)[name] == data
    stored = hubops._load_state(svc(served)).closing  # noqa: SLF001
    assert stored == {k: n["o"][k] for k in ("removal_id", "issued_ms", "ends_ms", "binding_seq")}
    assert audit_codes(served) == []


def test_no_new_company_grant_while_it_stands(served, org, cl, accepted):
    org.skills = [org.skill("invoices"), org.skill("support", "Greet people by name.")]
    org.versions["skill"] = 2
    sync(served)
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    res = req(served, "org.accept", {"kind": "skill", "name": "support",
                                      "digest": OB.skill_digest("support", "Greet people by name.")})  # fmt: skip
    assert res["ok"] is False and "being removed" in res["error"]["message"]
    assert orgstate.served(svc(served)) == {"skills": [], "mcp": []}


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda org, served: notice(org, served, key=suite.generate_private_key()), "bad_signature"),
        (lambda org, served: notice(org, served, binding_seq=2), "stale_binding"),
        (lambda org, served: notice(org, served, workspace="ab" * 16), "other_workspace"),
        (
            lambda org, served: notice(org, served, ends=now_ms(served) - 1, issued=now_ms(served) - 5000),
            "cert_expired",
        ),
        (lambda org, served: notice(org, served, ends=now_ms(served) + 8 * DAY), "cert_invalid"),
        (lambda org, served: {"o": {"kind": "hub.closing"}, "sig": "x"}, None),
    ],
)
def test_a_forged_or_wrong_binding_notice_is_ignored_with_its_code(served, org, cl, accepted, make, code):
    cl.reply = {"closing": make(org, served)}
    sync(served)
    assert status(served)["closing"] is None
    assert [s["name"] for s in orgstate.served(svc(served))["skills"]] == ["invoices"]
    got = audit_codes(served)
    assert len(got) == 1 and (got[0] == code if code else got[0] in ("malformed", "bad_signature", "wrong_suite"))
    assert hubops._load_state(svc(served)).closing is None  # noqa: SLF001


def test_a_refused_notice_does_not_clear_a_held_one(served, org, cl, accepted):
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    cl.reply = {"closing": notice(org, served, binding_seq=9)}
    sync(served)
    assert status(served)["closing"] is not None and audit_codes(served) == ["stale_binding"]


def test_a_null_answer_clears_it_and_serving_resumes(served, org, cl, accepted):
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    assert orgstate.served(svc(served)) == {"skills": [], "mcp": []}
    cl.reply = {"closing": None}  # a restored hub
    sync(served)
    assert status(served)["closing"] is None
    assert [s["name"] for s in orgstate.served(svc(served))["skills"]] == ["invoices"]


@pytest.mark.parametrize(
    "bad", [{}, {"closing": "x"}, {"closing": []}, {"closing": None, "x": 1}, {"notice": None}, [], "closing"]
)
def test_the_pull_reply_shape_is_strict_and_a_bad_one_changes_nothing(served, org, cl, accepted, bad):
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    held = hubops._load_state(svc(served)).closing  # noqa: SLF001
    cl.reply = bad  # neither clears the held notice nor sets one
    sync(served)
    assert hubops._load_state(svc(served)).closing == held  # noqa: SLF001
    cl.reply = {"closing": None}
    sync(served)
    cl.reply = bad
    sync(served)
    assert hubops._load_state(svc(served)).closing is None  # noqa: SLF001


def test_nothing_happens_at_ends_ms(served, org, cl, accepted):
    n = notice(org, served)
    cl.reply = {"closing": n}
    sync(served)
    before = files(served)
    served.lab.ws.clock[0] = (n["o"]["ends_ms"] + 5 * DAY) / 1000  # long past the end, no hub answer
    assert status(served)["closing"] == {"since_ms": n["o"]["issued_ms"], "ends_ms": n["o"]["ends_ms"]}
    assert orgstate.served(svc(served)) == {"skills": [], "mcp": []}
    assert files(served) == before  # nothing purged, nothing left


def test_a_leave_clears_it(human, served, org, cl, accepted):
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    state = hubops._load_state(svc(served))  # noqa: SLF001
    assert state.status == "unbound" and state.closing is None
    assert hubclosing.active(state) is None


def test_a_record_of_another_binding_counts_for_nothing(served, org, cl, accepted):
    cl.reply = {"closing": notice(org, served)}
    sync(served)
    state = hubops._load_state(svc(served))  # noqa: SLF001
    assert hubclosing.active(state) is not None
    state.binding_seq += 1  # a rebind: the record is of the old binding
    assert hubclosing.active(state) is None and hubclosing.view(state) is None


def test_a_pushed_notice_is_verified_recorded_and_acknowledged_with_an_empty_body(served, org, cl, accepted):
    n = notice(org, served)
    cl.push_next = n
    link = peerops._open_link(svc(served))  # noqa: SLF001
    try:
        link.call("closing_get", {})  # the push arrives before the reply
        assert hubclosing.pump(svc(served), link, 0.0) is True
        assert hubclosing.pump(svc(served), link, 0.0) is False
    finally:
        link.close()
    deadline = time.time() + 3
    while not cl.acks and time.time() < deadline:
        time.sleep(0.02)
    assert cl.acks and cl.acks[0]["op"] == "closing_notice" and cl.acks[0]["body"] == {}
    assert status(served)["closing"]["ends_ms"] == n["o"]["ends_ms"]
    assert orgstate.served(svc(served)) == {"skills": [], "mcp": []}


def test_a_forged_pushed_notice_is_acknowledged_with_its_code_and_changes_nothing(served, org, cl, accepted):
    cl.push_next = notice(org, served, binding_seq=4)
    link = peerops._open_link(svc(served))  # noqa: SLF001
    try:
        link.call("closing_get", {})
        assert hubclosing.pump(svc(served), link, 0.0) is True
    finally:
        link.close()
    deadline = time.time() + 3
    while not cl.acks and time.time() < deadline:
        time.sleep(0.02)
    assert cl.acks[0]["body"] == {"error": "stale_binding"}
    assert status(served)["closing"] is None and audit_codes(served) == ["stale_binding"]
