"""R13: the AI Factory from a paired device, layer by layer. The real bridge host (scope hook, assertion check, replay
record), the real dashboard app behind it and the real ledger writes; only the transport child is absent."""
import asyncio
import hashlib
import json
import re
import struct

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

import test_bridge_host_vectors as V  # noqa: E402
from orch.core import epics, factory_sessions, ledger, permits, store  # noqa: E402
from orch.dashboard.app import create_app, dashboard_routes  # noqa: E402
from orch.dashboard.bridge_dispatch import BridgeRequest, Refused, Start, dispatch  # noqa: E402
from orch.dashboard.bridge_loop import route_hook  # noqa: E402
from orch.dashboard.reach import RemoteOrigin, Scope  # noqa: E402
from orch.remote.bridge_host import envelope as E, files  # noqa: E402
from orch.remote.bridge_host.assertion import AD_UP, AD_UV, assertion_challenge  # noqa: E402
from orch.remote.bridge_host.host_check import Host  # noqa: E402
from orch.remote.bridge_host.registry import Device  # noqa: E402
from test_bridge_host import AUTH, CRED_ID, KEY_A, KEY_B, NEW, NOW, WS, WS_HEX, credential, did, env, pub, rid_of  # noqa: E402
from test_factory_dashboard import CMD, PROOF, _refine, _seen, fa, fd, fh, fws, ready_epic, running  # noqa: E402,F401

FORM = {"content-type": "application/x-www-form-urlencoded"}
OTHER_SHA = "sha256:" + "0" * 64


class Bridge:
    """A host with three devices: A (Type), B (Decide) and C (Operate), each with an authenticator."""

    def __init__(self, tmp_path, ws):
        self.ws, self.clock = ws, V.Clock(NOW)
        self.app = create_app(ws, "tok")
        self.host = Host(workspace=WS, k_ws=V.K_WS, host_key=__import__("test_bridge_host").signatures.private_key(
            bytes.fromhex(V.VEC["keys"]["host"]["d"])), root=files.bridge_dir(tmp_path, WS_HEX), clock=self.clock,
            route=route_hook(dashboard_routes(), ws), phone_key=lambda pid: None, rp_id=V.RP_ID, origin=V.ORIGIN)
        for key, scope in ((KEY_A, "type"), (KEY_B, "decide"), (NEW, "operate")):
            self.host.registry.add(Device(did(key), pub(key), scope, "Phone " + scope, 0, credential=credential()), 0)
        self.seq: dict = {}
        self.count: dict = {}

    def _env(self, key, meta, data=b""):
        self.seq[did(key)] = self.seq.get(did(key), 0) + 1
        self.clock.now += 1000
        return env(key, meta, data, seq=self.seq[did(key)], ts=self.clock.now)

    def _decide(self, e):
        acc = self.host.check(e, rid_of(e))
        return self.host.authorize(acc) if acc.result == "accept" else acc

    def post(self, key, path, body: str = "", method="POST"):
        """(verdict, envelope) of one request: a `run`, or a refusal (assertion_required included)."""
        e = self._env(key, {"op": "http", "method": method, "path": path, "headers": FORM}, body.encode())
        return self._decide(e), e

    def answer(self, key, refusal, r1, *, subject=None, for_rid=None):
        """R2: the authenticator signs the challenge of `refusal` (over `subject` when given: a forged one)."""
        f = refusal.fields
        self.count[did(key)] = self.count.get(did(key), 0) + 1
        ch = assertion_challenge(WS, bytes.fromhex(did(key)), bytes.fromhex(rid_of(r1)), f["purpose"], f["scope"],
                                 f["expires_ms"], bytes.fromhex(f["nonce"]), subject or f["subject"])
        ad = hashlib.sha256(V.RP_ID.encode()).digest() + bytes([AD_UP | AD_UV]) + struct.pack(">I", self.count[did(key)])
        cdj = E.canonical_json({"type": "webauthn.get", "challenge": E.b64u(ch), "origin": V.ORIGIN, "crossOrigin": False})
        der = AUTH.sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        meta = {"op": "assert", "for": for_rid or rid_of(r1), "credential_id": E.b64u(CRED_ID),
                "authenticator_data": E.b64u(ad), "client_data_json": E.b64u(cdj), "signature": E.b64u(der)}
        e = self._env(key, meta)
        return self._decide(e), e

    def run(self, v, fresh=None):
        """Run an authorised decision against the dashboard as the loop does: (status, location)."""
        m = v.meta
        from orch.dashboard.bridge_loop import origin_for
        origin = origin_for(v)

        async def go():
            status, headers = None, {}
            async for ev in dispatch(self.app, BridgeRequest(m["method"], m["path"], m.get("headers", {}), v.data),
                                     origin, still_authorized=lambda: self.host.still_authorized(v)):
                if isinstance(ev, Start):
                    status, headers = ev.status, dict(ev.headers)
                elif isinstance(ev, Refused):
                    return ev.reason, ""
            return status, headers.get("location", "")
        return asyncio.run(go())

    def raw(self, scope, fresh, method, path, body=""):
        """One request straight into the app with an origin the test builds: (status, location)."""
        origin = RemoteOrigin("dev_raw", scope, "Raw", fresh)

        async def go():
            status, headers = None, {}
            async for ev in dispatch(self.app, BridgeRequest(method, path, FORM, body.encode()), origin,
                                     still_authorized=lambda: True):
                if isinstance(ev, Start):
                    status, headers = ev.status, dict(ev.headers)
            return status, headers.get("location", "")
        return asyncio.run(go())

    def raw_body(self, scope, path):
        return self.raw(scope, False, "GET", path)

    def fresh(self, key, path, body):
        """The whole round: request, assertion, run. (refusal verdict, run verdict or None, status, location)."""
        v, r1 = self.post(key, path, body)
        if v.code != "assertion_required":
            return v, None, None, ""
        run, _ = self.answer(key, v, r1)
        if run.result != "run":
            return v, run, None, ""
        status, loc = self.run(run)
        return v, run, status, loc


@pytest.fixture
def bridge(tmp_path, fws):
    return Bridge(tmp_path, fws)


def _grant_body(r, scope="once", sha=None):
    return f"sha={sha or r['sha']}&scope={scope}&next=%2F"


# -- Allow: Type + a fresh assertion over the exact request ------------------------------------------------------------

def test_allow_runs_only_after_an_assertion_over_the_exact_request_and_names_the_device(bridge, fws, running):
    eid, cid, r = running
    path = f"/permits/{r['id']}/grant"
    refusal, r1 = bridge.post(KEY_A, path, _grant_body(r, "epic"))
    assert refusal.code == "assertion_required"
    sub = refusal.fields["subject"]
    assert sub["kind"] == "permission" and re.fullmatch("[0-9a-f]{64}", sub["digest"])
    assert permits.shown(CMD) in sub["shown"] and "for the whole epic" in sub["shown"] and r["id"] in sub["shown"]
    assert not [e for e in ledger.entries(fws) if e["kind"] == "grant"]  # nothing written before the assertion
    run, _ = bridge.answer(KEY_A, refusal, r1)
    assert run.result == "run" and run.fresh
    status, loc = bridge.run(run)
    assert status == 303 and "err=" not in loc
    grants = [e for e in ledger.entries(fws) if e["kind"] == "grant"]
    assert len(grants) == 1 and grants[0]["scope"] == "epic" and grants[0]["device"] == did(KEY_A)
    assert (r["id"], r["sha"]) in permits.decisions(fws)


def test_a_decide_device_cannot_allow_and_a_type_device_without_an_assertion_cannot_either(bridge, fws, running):
    eid, cid, r = running
    path = f"/permits/{r['id']}/grant"
    v, _ = bridge.post(KEY_B, path, _grant_body(r))
    assert v.code == "forbidden_scope"  # Decide is below Type: no challenge is even issued
    v, _ = bridge.post(NEW, path, _grant_body(r))
    assert v.code == "forbidden_scope"  # Operate too
    # a request that reached the app without an assertion (the origin is not fresh) is refused by the gate itself
    assert bridge.raw(Scope.TYPE, False, "POST", path, _grant_body(r))[0] == 403
    assert not [x for x in ledger.entries(fws) if x["kind"] == "grant"]


def test_deny_revoke_and_pause_need_only_decide(bridge, fws, running):
    eid, cid, r = running
    v, _ = bridge.post(KEY_B, f"/permits/{r['id']}/deny", f"sha={r['sha']}&next=%2F")
    assert v.result == "run" and not v.fresh
    status, loc = bridge.run(v)
    assert status == 303 and "err=" not in loc
    deny = [e for e in ledger.entries(fws) if e["kind"] == "permit_deny"]
    assert len(deny) == 1 and deny[0]["device"] == did(KEY_B)
    v, _ = bridge.post(KEY_B, f"/t/{eid}/epic/pause")  # Pause (= Stop): no assertion
    assert v.result == "run"
    status, loc = bridge.run(v)
    assert status == 303 and epics.delegation(fws, store.load(fws, eid)[1])["paused"]


# -- an assertion binds one request: another request, another hash, a replay, a stale state ----------------------------

def test_an_assertion_for_one_request_does_not_release_another(bridge, fws, running, fa):
    eid, cid, r = running
    r2 = permits.request(fws, fa.actor, store.load(fws, cid)[1], "make other", reason="x")
    v1, e1 = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    v2, e2 = bridge.post(KEY_A, f"/permits/{r2['id']}/grant", _grant_body(r2))
    assert v1.fields["subject"] != v2.fields["subject"]
    bad, _ = bridge.answer(KEY_A, v1, e1, for_rid=rid_of(e2))  # signed for request 1, offered for request 2
    assert bad.code == "assertion_failed"
    assert not [e for e in ledger.entries(fws) if e["kind"] == "grant"]


def test_an_assertion_over_a_different_subject_is_refused(bridge, fws, running):
    eid, cid, r = running
    v, e = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    forged = {**v.fields["subject"], "digest": "1" * 64}
    bad, _ = bridge.answer(KEY_A, v, e, subject=forged)
    assert bad.code == "assertion_failed"
    assert not [x for x in ledger.entries(fws) if x["kind"] == "grant"]


def test_a_replayed_assertion_does_not_run_twice(bridge, fws, running):
    eid, cid, r = running
    v, e = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    run, e2 = bridge.answer(KEY_A, v, e)
    assert run.result == "run"
    bridge.run(run)
    bridge.host.finish(run.answer_rids, {"status": 303, "headers": []}, b"")
    n = len(ledger.entries(fws))
    assert bridge.host.check(e2, rid_of(e2)).result == "replay"  # the same bytes: the stored outcome
    again, e3 = bridge.answer(KEY_A, v, e)  # the same assertion in a new envelope: its challenge is spent
    assert again.code == "assertion_failed" and len(ledger.entries(fws)) == n


def test_a_stale_or_unknown_request_gets_no_challenge(bridge, fws, running):
    eid, cid, r = running
    for path, body in ((f"/permits/{r['id']}/grant", _grant_body(r, sha=OTHER_SHA)),  # not the command shown
                       (f"/permits/{r['id']}/grant", "scope=once"),  # no sha at all
                       ("/permits/P-FFFFFFFF/grant", _grant_body(r)),  # no such request
                       (f"/permits/{r['id']}/grant", _grant_body(r, scope="forever"))):
        v, _ = bridge.post(KEY_A, path, body)
        assert v.code == "assertion_failed", (path, body)
    v, e = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    permits.permit_deny(fws, __import__("orch.dashboard.reach", fromlist=["LOCAL_HUMAN"]).LOCAL_HUMAN, r["id"], expected_sha=r["sha"])
    run, _ = bridge.answer(KEY_A, v, e)  # answered meanwhile: the write itself refuses
    status, loc = bridge.run(run)
    assert "err=" in loc and not [x for x in ledger.entries(fws) if x["kind"] == "grant"]
    v, _ = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    assert v.code == "assertion_failed"  # an answered request gets no new challenge


def test_the_host_being_away_means_nothing_queued(bridge, fws, running):
    eid, cid, r = running
    v, e = bridge.post(KEY_A, f"/permits/{r['id']}/grant", _grant_body(r))
    bridge.host.stop()  # the kill switch / host away: the assertion cannot release anything
    run, _ = bridge.answer(KEY_A, v, e)
    assert run.result != "run" and not [x for x in ledger.entries(fws) if x["kind"] == "grant"]


# -- Start: the existing approve route with the factory limits -------------------------------------------------------------

def _epic(fws, fa):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    return e.id


def _seen_charter(fws, eid):
    return epics.charter(fws, store.load(fws, eid)[1])["content_hash"]


def test_start_from_a_device_signs_the_charter_arms_the_runner_and_records_the_device(bridge, fws, fa):
    eid = _epic(fws, fa)
    seen = _seen_charter(fws, eid)
    body = f"gate=requirements&seen={seen}&factory=1&next=%2F"
    v, e = bridge.post(KEY_B, f"/t/{eid}/approve", body)
    assert v.code == "forbidden_scope"  # Decide cannot Start
    refusal, run, status, loc = bridge.fresh(KEY_A, f"/t/{eid}/approve", body)
    assert refusal.fields["subject"]["kind"] == "charter" and re.fullmatch("[0-9a-f]{64}", refusal.fields["subject"]["digest"])
    assert eid in refusal.fields["subject"]["shown"] and "max_children 25" in refusal.fields["subject"]["shown"]
    assert status == 303 and "err=" not in loc
    d = epics.delegation(fws, store.load(fws, eid)[1])
    assert d["factory"] and d["active"]
    charter = epics.latest_charter(fws, eid)
    assert charter["device"] == did(KEY_A)  # the signed ledger entry names the device
    assert factory_sessions.armed(fws, d["id"])
    marker = factory_sessions._root() / "armed" / factory_sessions._key(fws, d["id"])
    assert json.loads(marker.read_text())["device"] == did(KEY_A)  # and so does the armed marker


def test_start_with_a_stale_charter_gets_no_challenge_and_an_ordinary_approval_stays_decide(bridge, fws, fa):
    eid = _epic(fws, fa)
    v, _ = bridge.post(KEY_A, f"/t/{eid}/approve", f"gate=requirements&seen={OTHER_SHA}&factory=1")
    assert v.code == "assertion_failed"
    v, _ = bridge.post(KEY_A, f"/t/{eid}/approve", "gate=requirements&factory=1")  # no hash at all
    assert v.code == "assertion_failed"
    assert epics.delegation(fws, store.load(fws, eid)[1]) is None
    v, _ = bridge.post(KEY_B, f"/t/{eid}/approve", f"gate=requirements&seen={_seen_charter(fws, eid)}")
    assert v.result == "run"  # no factory limits: Decide, no assertion, as before


def test_start_whose_epic_changed_after_the_assertion_writes_nothing(bridge, fws, fa):
    eid = _epic(fws, fa)
    body = f"gate=requirements&seen={_seen_charter(fws, eid)}&factory=1"
    v, e = bridge.post(KEY_A, f"/t/{eid}/approve", body)
    run, _ = bridge.answer(KEY_A, v, e)
    fa.set_section(eid, "Requirements", "changed after you looked")
    status, loc = bridge.run(run)
    assert "err=" in loc and epics.delegation(fws, store.load(fws, eid)[1]) is None


# -- editing under a running epic ------------------------------------------------------------------------------------------

def test_editing_under_a_running_epic_needs_type_and_an_assertion_and_commenting_an_assertion_at_operate(bridge, fws, running):
    eid, cid, r = running
    v, _ = bridge.post(NEW, f"/t/{cid}/edit", "text=x")
    assert v.code == "forbidden_scope"  # Operate is below Type here
    refusal, run, status, loc = bridge.fresh(KEY_A, f"/t/{cid}/task/add", "text=new+step")
    assert refusal.fields["subject"]["kind"] == "action" and "text=new+step" in refusal.fields["subject"]["shown"]
    assert run.result == "run" and status != 403
    refusal, run, status, loc = bridge.fresh(NEW, f"/t/{cid}/comment", "text=hello")  # Operate scope, with an assertion
    assert refusal.code == "assertion_required" and status == 303
    v, _ = bridge.post(NEW, f"/t/{cid}/epic/pause")
    assert v.result == "run"  # pausing is Decide: an Operate device may, with no assertion


# -- the epic's done verdict ---------------------------------------------------------------------------------------------

def test_the_epic_verdict_needs_type_and_an_assertion_over_the_hash_the_ready_report_carries(bridge, fws, fd, ready_epic):
    eid, cid = ready_epic
    seen = _seen(fd)
    body = f"verdict=done&seen={seen}&next=%2F"
    v, _ = bridge.post(KEY_B, f"/t/{eid}/verdict", body)
    assert v.code == "forbidden_scope"  # Decide may not sign an epic's verdict
    refusal, run, status, loc = bridge.fresh(KEY_A, f"/t/{eid}/verdict", body)
    sub = refusal.fields["subject"]
    assert sub["kind"] == "verdict" and re.fullmatch("[0-9a-f]{64}", sub["digest"]) and cid in sub["shown"]
    assert status == 303 and "err=" not in loc
    assert store.load(fws, eid)[1].status == "done" and store.load(fws, cid)[1].status == "done"
    verdicts = [e for e in ledger.entries(fws) if e["kind"] == "verdict" and e["ticket"] in (eid, cid)]
    assert verdicts and all(e.get("device") == did(KEY_A) for e in verdicts)


def test_an_epic_verdict_over_a_stale_hash_gets_no_challenge_and_a_change_after_it_writes_nothing(bridge, fws, fa, ready_epic, fd):
    eid, cid = ready_epic
    v, _ = bridge.post(KEY_A, f"/t/{eid}/verdict", f"verdict=done&seen={OTHER_SHA}")
    assert v.code == "assertion_failed"
    seen = _seen(fd)
    v, e = bridge.post(KEY_A, f"/t/{eid}/verdict", f"verdict=done&seen={seen}")
    run, _ = bridge.answer(KEY_A, v, e)
    fa.set_section(cid, "Verification", PROOF + "\n- AC1: changed after you read it")
    status, loc = bridge.run(run)
    assert "err=" in loc and store.load(fws, eid)[1].status == "open"


def test_an_epic_verdict_other_than_done_gets_no_challenge(bridge, fws, fd, ready_epic):
    eid, cid = ready_epic
    v, _ = bridge.post(KEY_A, f"/t/{eid}/verdict", f"verdict=follow-up&seen={_seen(fd)}")
    assert v.code == "assertion_failed"


# -- status data and the two background tasks ------------------------------------------------------------------------------

def test_the_factory_status_reaches_a_look_device_and_the_heartbeat(bridge, fws, running):
    from orch.remote import presence
    eid, cid, r = running
    state = presence.factory(fws)
    assert state["factory"] == "waiting" and state["children_total"] == 1 and "budget_pct" in state
    for path in ("/", "/board", f"/t/{eid}"):  # the cards a Look device reads: nothing needs a higher scope
        status, _ = bridge.raw(Scope.LOOK, False, "GET", path)
        assert status == 200, path


def test_the_runner_and_the_bridge_loop_run_side_by_side_in_the_lifespan(ws, monkeypatch):
    from orch.dashboard import factory_runner

    started = {}

    async def runner(w, *a, **k):
        started["runner"] = True
        await asyncio.sleep(3600)

    class FakeLoop:
        host, link = None, None

        async def run(self):
            started["bridge"] = True
            await asyncio.sleep(3600)

        async def stop(self):
            started["stopped"] = True

    monkeypatch.setattr(factory_runner, "loop", runner)
    app = create_app(ws, "tok", remote=lambda a: FakeLoop())

    async def main():
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if started.get("runner") and started.get("bridge"):
                    break
                await asyncio.sleep(0.02)
            assert started.get("runner") and started.get("bridge")  # both tasks alive at once
        assert started.get("stopped")
    asyncio.run(main())


# -- one parse: what is shown is what the route reads -----------------------------------------------------------------------

def test_a_field_twice_or_only_in_the_query_gets_no_challenge(bridge, fws, fa, running):
    eid, cid, r = running
    path = f"/permits/{r['id']}/grant"
    for body, p in ((f"sha={OTHER_SHA}&sha={r['sha']}&scope=once", path), (f"sha={r['sha']}&sha={r['sha']}", path),
                    (f"scope=once", path + f"?sha={r['sha']}"), (f"sha={r['sha']}&scope=once&scope=epic", path)):
        v, _ = bridge.post(KEY_A, p, body)
        assert v.code == "assertion_failed", (body, p)
    e2 = _epic(fws, fa)
    seen = _seen_charter(fws, e2)
    v, _ = bridge.post(KEY_A, f"/t/{e2}/approve", f"gate=requirements&seen={seen}&factory=0&factory=1")
    assert v.code == "assertion_failed"
    v, _ = bridge.post(KEY_A, f"/t/{e2}/approve?seen={seen}", "gate=requirements&factory=1")
    assert v.code == "assertion_failed"


def test_the_charter_shown_is_what_the_route_does(bridge, fws, fa):
    e2 = _epic(fws, fa)
    seen = _seen_charter(fws, e2)
    # "factory=yes" arms nothing in the route: the person is told it is an approval with no delegation
    v, e = bridge.post(KEY_A, f"/t/{e2}/approve", f"gate=requirements&seen={seen}&factory=yes")
    assert "no delegation" in v.fields["subject"]["shown"] and "AI Factory" not in v.fields["subject"]["shown"]
    run, _ = bridge.answer(KEY_A, v, e)
    bridge.run(run)
    assert not (epics.delegation(fws, store.load(fws, e2)[1]) or {}).get("factory")
    e3 = _epic(fws, fa)
    v, _ = bridge.post(KEY_A, f"/t/{e3}/approve", f"gate=requirements&seen={_seen_charter(fws, e3)}&factory=1&max_children=3")
    assert "AI Factory" in v.fields["subject"]["shown"] and "max_children 25" in v.fields["subject"]["shown"]
    v, _ = bridge.post(KEY_A, f"/t/{e3}/approve", f"gate=plans&seen={_seen_charter(fws, e3)}&factory=1")
    assert v.code == "assertion_failed"


def test_an_action_body_that_is_not_text_cannot_be_approved(bridge, fws, running):
    eid, cid, r = running
    e = bridge._env(KEY_A, {"op": "http", "method": "POST", "path": f"/t/{cid}/edit", "headers": FORM}, b"text=\xff\xfe")
    assert bridge._decide(e).code == "assertion_failed"


def test_a_subject_always_carries_a_digest():
    from orch.dashboard.factory_remote import _subject
    assert _subject("verdict", "x", "") is None and _subject("verdict", "x", "AB" * 32) is None
    assert _subject("verdict", "x", "a" * 64)["digest"] == "a" * 64


# -- the subject covers every field that changes what the action does ------------------------------------------------------

def _subj(bridge, key, path, body):
    v, _ = bridge.post(key, path, body)
    assert v.code == "assertion_required", (path, body, v.code)
    return v.fields["subject"]


def test_every_field_of_an_allow_changes_the_subject(bridge, fws, fa, running):
    eid, cid, r = running
    r2 = permits.request(fws, fa.actor, store.load(fws, cid)[1], "make other", reason="x")
    base = _subj(bridge, KEY_A, f"/permits/{r['id']}/grant", _grant_body(r, "once"))
    for other in (_subj(bridge, KEY_A, f"/permits/{r['id']}/grant", _grant_body(r, "epic")),  # duration
                  _subj(bridge, KEY_A, f"/permits/{r2['id']}/grant", _grant_body(r2, "once"))):  # request / command
        assert other["digest"] != base["digest"] and other["shown"] != base["shown"]


def test_every_field_of_a_start_changes_the_subject(bridge, fws, fa):
    e1, e2 = _epic(fws, fa), _epic(fws, fa)
    body = lambda e, extra="": f"gate=requirements&seen={_seen_charter(fws, e)}{extra}"
    base = _subj(bridge, KEY_A, f"/t/{e1}/approve", body(e1, "&factory=1"))
    others = [_subj(bridge, KEY_A, f"/t/{e2}/approve", body(e2, "&factory=1")),  # the epic
              _subj(bridge, KEY_A, f"/t/{e1}/approve", body(e1, "&delegate=1&max_children=3")),  # the limits
              _subj(bridge, KEY_A, f"/t/{e1}/approve", body(e1, "&delegate=1&max_children=4")),
              _subj(bridge, KEY_A, f"/t/{e1}/approve", body(e1, "&factory=yes"))]  # no delegation at all
    assert len({o["digest"] for o in others + [base]}) == 5
    fa.set_section(e1, "Requirements", "other text")  # the epic text moved: no challenge for the old hash
    v, _ = bridge.post(KEY_A, f"/t/{e1}/approve", body(e1, "&factory=1").replace(_seen_charter(fws, e1), OTHER_SHA))
    assert v.code == "assertion_failed"


def test_every_field_of_a_verdict_changes_the_subject(bridge, fws, fd, ready_epic):
    eid, cid = ready_epic
    seen = _seen(fd)
    base = _subj(bridge, KEY_A, f"/t/{eid}/verdict", f"verdict=done&seen={seen}")
    noted = _subj(bridge, KEY_A, f"/t/{eid}/verdict", f"verdict=done&seen={seen}&message=hello")
    assert noted["digest"] != base["digest"] and noted["shown"] != base["shown"]
    single = _subj(bridge, KEY_B if False else KEY_A, f"/t/{cid}/verdict", f"verdict=done&seen={epics.verdict_hash([store.load(fws, cid)[1]], fws)}")
    assert single["digest"] != base["digest"] and cid in single["shown"]


# -- the gate is at least as strict as main for every route that existed ----------------------------------------------------

def test_the_tags_table_is_at_least_as_strict_as_mains():
    """tests/data/main_remote_tags.json is main's TAGS table before R13: every (method, route) with the (scope, fresh,
    kind) of each probe of the conditional tags. Regenerate it only for a deliberate change to the table."""
    from pathlib import Path
    from orch.dashboard import remote_gate
    snap = json.loads((Path(__file__).parent / "data" / "main_remote_tags.json").read_text())
    probes = [None, {}, {"factory": ["1"]}, {"delegate": ["1"]}, {"max_children": ["3"]}, {"max_size": ["s"]}]
    for key, rows in snap.items():
        method, path = key.split(" ", 1)
        assert (method, path) in remote_gate.TAGS, f"{key} was tagged on main"
        now = remote_gate.TAGS[(method, path)]
        for params, (scope, fresh, kind) in zip(probes if callable(now) or len(rows) > 1 else [None], rows):
            b = now(params) if callable(now) else now
            if scope is None:
                assert b.scope is None, (key, params)  # never remote stays never remote
            else:
                assert b.scope is not None and b.scope >= scope and (b.fresh or not fresh) and b.kind == kind, (key, params)


def test_factory_need_loosens_only_pause_against_mains_rule():
    """On main every POST under /t/{ref} on a factory epic or its child needed a fresh assertion. Now each still does,
    at Type, except a comment (its own scope, Operate) and the pause, which stays Decide with none."""
    from orch.dashboard import remote_gate
    assert remote_gate.FACTORY_OPEN == ("/t/{ref}/epic/pause",)
    orig = remote_gate.factory_guarded
    remote_gate.factory_guarded = lambda ws, ref: True  # stand in for a ticket under a running epic
    try:
        for (method, path), tag in remote_gate.TAGS.items():
            if method != "POST" or not path.startswith("/t/{ref}/") or path in remote_gate.FACTORY_OPEN:
                continue
            tag = tag(None) if callable(tag) else tag
            if tag.scope is None:
                continue
            need = remote_gate.factory_need(None, "POST", path, {"ref": "x"}, tag)
            want = tag.scope if path == "/t/{ref}/comment" else remote_gate.Scope.TYPE
            assert need is not None and need[0] == want, path
    finally:
        remote_gate.factory_guarded = orig


def test_the_gate_itself_refuses_an_operate_origin_an_edit_even_with_a_fresh_assertion(bridge, fws, running):
    eid, cid, r = running
    assert bridge.raw(Scope.OPERATE, True, "POST", f"/t/{cid}/edit", "text=x")[0] == 403
    assert bridge.raw(Scope.TYPE, True, "POST", f"/t/{cid}/edit", "text=x")[0] != 403


# -- review round: Decide routes under a running epic, extra Start fields, body shapes, the fresh backstop -----------------

def test_a_decide_device_cannot_close_answer_move_or_approve_under_a_running_epic_even_with_an_assertion(bridge, fws, running):
    eid, cid, r = running
    cases = (("close", "as=wont-do&message=x"), ("answer", "qid=q1&qhash=x&value=y"), ("request-changes", "gate=plan&seen=x"),
             ("move", "to=backlog"), ("approve", "gate=requirements&seen=x"), ("reopen", ""), ("option", "option=a%2Fb&value=1"),
             ("approve-together", "seen=x&seen_plan=y"), ("verdict", "verdict=done&seen=x"))
    for action, body in cases:
        v, _ = bridge.post(KEY_B, f"/t/{cid}/{action}", body)
        assert v.code == "forbidden_scope", action  # no challenge: Decide is below Type
        assert bridge.raw(Scope.DECIDE, True, "POST", f"/t/{cid}/{action}", body)[0] == 403, action  # nor at the gate
    assert store.load(fws, cid)[1].status != "done"  # nothing above ran
    assert bridge.raw(Scope.TYPE, True, "POST", f"/t/{cid}/close", "as=wont-do&message=x")[0] != 403


def test_a_start_with_fields_that_are_not_shown_is_refused(bridge, fws, fa):
    e = _epic(fws, fa)
    base = f"gate=requirements&seen={_seen_charter(fws, e)}&factory=1"
    assert bridge.post(KEY_A, f"/t/{e}/approve", base)[0].code == "assertion_required"
    for extra in ("&despite_open_question=1", "&option_offered=a%2Fb", "&option_on=a%2Fb"):
        assert bridge.post(KEY_A, f"/t/{e}/approve", base + extra)[0].code == "assertion_failed", extra


def test_a_body_the_route_would_read_differently_gets_no_challenge(bridge, fws, fa, running):
    eid, cid, r = running
    e = _epic(fws, fa)
    seen = _seen_charter(fws, e)
    assert bridge.post(KEY_A, f"/t/{e}/approve", f"gate=requirements&seen={seen}&x=1&factory=1;delegate=1")[0].code == "assertion_failed"
    v, _ = bridge.post(KEY_A, f"/t/{eid}/verdict", "verdict=done&seen=x&message=é")
    assert v.code == "assertion_failed"
    ev = bridge._env(KEY_A, {"op": "http", "method": "POST", "path": f"/t/{cid}/comment", "headers": FORM},
                     "text=é".encode())
    assert bridge._decide(ev).code == "assertion_failed"  # raw non-ASCII bytes: not shown the way they are written


def test_the_gates_fresh_backstop_holds_where_the_hook_asks_for_no_assertion(bridge, fws, fa, running):
    # a Type device that skipped the assertion: schedule arm (no subject builder) and a multipart Start (unreadable form)
    v, _ = bridge.post(KEY_A, "/schedules/s1/arm", "")
    assert v.result == "run" and not v.fresh
    assert bridge.run(v)[0] == 403
    e = _epic(fws, fa)
    meta = {"op": "http", "method": "POST", "path": f"/t/{e}/approve",
            "headers": {"content-type": "multipart/form-data; boundary=x"}}
    body = (f'--x\r\nContent-Disposition: form-data; name="factory"\r\n\r\n1\r\n--x\r\nContent-Disposition: form-data; '
            f'name="gate"\r\n\r\nrequirements\r\n--x--\r\n').encode()
    v = bridge._decide(bridge._env(KEY_A, meta, body))
    assert v.code == "assertion_failed"  # the strictest tag: a fresh assertion is asked for, but there is nothing to show
    assert bridge.raw(Scope.TYPE, False, "POST", f"/t/{e}/approve", "gate=requirements&factory=1")[0] == 403
    assert epics.delegation(fws, store.load(fws, e)[1]) is None


def test_the_route_hook_is_never_less_strict_than_the_gate_for_every_route_and_body_shape(fws, running):
    """Table-driven from remote_gate.TAGS: for each route and body shape the gate would decide (its own parse), the hook's
    scope is at least the gate's, and a route the gate never allows is never remote for the hook."""
    from urllib.parse import parse_qs, parse_qsl
    from orch.dashboard import remote_gate as G
    eid, cid, r = running
    routes = dashboard_routes()
    hook = route_hook(routes, fws)
    form = {"content-type": "application/x-www-form-urlencoded"}
    shapes = [({}, b""), (form, b""), (form, b"factory=1&seen=x&gate=requirements"), (form, b"delegate=1&max_children=3"),
              (form, b"factory=1;delegate=1"), (form, "text=é&factory=1".encode()), (form, b"\xff\xfe=1&factory=1"),
              (form, b"factory=1&" + b"a=b&" * 20000), ({"content-type": "application/json"}, b'{"factory": 1}'),
              ({"content-type": "multipart/form-data; boundary=x"}, b"--x\r\n\r\nfactory\r\n--x--"), ({}, b"factory=1")]
    for (method, path), _tag in G.TAGS.items():
        for ref in (cid, eid):
            real = re.sub(r"\{[^}]*:path\}", "a.txt", path)
            real = re.sub(r"\{ref\}", ref, real)
            real = re.sub(r"\{[^}]*\}", "x1", real)
            for query in ("", "?factory=1"):
                for headers, body in shapes:
                    scope = {"type": "http", "method": method, "path": real, "raw_path": real.encode(),
                             "query_string": query[1:].encode(), "root_path": "",
                             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
                    params = None
                    if G.is_conditional(routes, scope):
                        if len(body) > G.MAX_PEEK:
                            continue  # the gate refuses it outright
                        if G._is_form(scope):
                            params = {}
                            for k, v in parse_qsl(query[1:], keep_blank_values=True):
                                params.setdefault(k, []).append(v)
                            for k, v in parse_qs(body.decode("utf-8", "replace").replace(";", "&"),
                                                 keep_blank_values=True).items():
                                params.setdefault(k, []).extend(v)
                    tag = G.tag_for(routes, scope, params)
                    got = hook({"op": "http", "method": method, "path": real + query, "headers": headers}, body)
                    if tag is None or tag.scope is None:
                        assert got is None, (method, real, headers, body[:20])
                        continue
                    need = None
                    if method == "POST":
                        need = G.factory_need(fws, method, G.match_route(routes, scope).path,
                                              G._match_params(routes, scope), tag)
                    want = max(tag.scope, need[0]) if need else tag.scope
                    assert got is not None and G.Scope[got.scope.upper()] >= want, (method, real, headers, body[:20])
