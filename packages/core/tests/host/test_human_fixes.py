"""H5a review round (#391): terminal escapes, the host-attested key and the uid fragment, half-applied submits, the
D58 recheck at submit, gate texts, the bound op, uploaded artifacts, the policy and gate_gen, device checks, quotas,
retention, expiry, question.stale and the redactor."""

from __future__ import annotations

import base64
import copy
import json
import os
from types import SimpleNamespace

import pytest

from orch import canon, crypto
from orch.host import human as host_human
from orch.host import review, signer
from orch.host.auth import Redactor
from orch.host.control_client import new_idem
from orch.host.errors import HostError
from orch.host.requests import GRANT_EXPIRE_SECONDS, MAX_PENDING
from orch.identity import certs
from orch.layout import Layout
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.test_human import TwoNotes, logged, outcome, ready, record, requests_dir, tick

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
REG = lambda w: ("Acme", "DEMO")  # noqa: E731


def reseal(p: dict) -> dict:
    """What a buggy or hostile host can always do: recompute the review, the digest and the fingerprint."""
    m = p["material"]
    m["review"] = [review.of(e, m) for e in p["events"]]
    p["review_digest"] = review.review_digest(m)
    p["fingerprint"] = review.fingerprint(p["review_digest"], p["events"], p["op"])
    return p


def refused(human, prepared, match=""):
    signed = len(human.backend.payloads)
    with pytest.raises(signer.SignerRefused, match=match):
        human.sign(prepared)
    assert len(human.backend.payloads) == signed


# --- 1. terminal escapes ----------------------------------------------------------------------------------------------

ESCAPES = ["\x1b[2J\x1b[H", "\r", "‮", "\x07", "\n"]


def test_the_clear_screen_probe_is_refused_before_rendering(human, served):
    _, key = human.ticket()
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    for esc in ESCAPES:
        bad = copy.deepcopy(p)
        bad["material"]["ticket"]["key"] = key + esc + "fingerprint: 000000000000"
        refused(human, reseal(bad), "malformed")


@pytest.mark.parametrize(
    "where",
    ["uid", "section", "before", "origin", "question"],
)
def test_every_identifier_from_the_material_is_checked_against_its_format(human, served, where):
    _, key = human.ticket()
    if where == "question":
        human.agent("ask", {"ref": key, "text": "q?", "options": ["a", "b"]})
        p = human.prepare("answer", {"ref": key, "question": "Q1", "option": "a"})
        p["material"]["question"]["id"] = "Q1\x1b[2J"
    elif where == "section":
        p = human.prepare("section.set", {"ref": key, "section": "summary", "message": "x"})
        p["material"]["texts"]["summary\x1b[2J"] = "x"
    elif where == "before":
        p = human.prepare("set", {"ref": key, "pairs": ["title=x"]})
        p["material"]["before"]["ticket.title\r"] = "A ticket"
    elif where == "origin":
        p = human.prepare("log", {"ref": key, "text": "x"})
        p["origin"]["created_at"] = "2026\x1b[2J"
    else:
        p = human.prepare("log", {"ref": key, "text": "x"})
        p["material"]["ticket"]["uid"] = p["log"][:-1] + "\x1b"
    refused(human, reseal(p))


def test_escape_characters_in_payload_values_are_shown_as_visible_markers(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    for esc in ESCAPES:
        bad = copy.deepcopy(p)
        bad["events"][0]["text"] = "before" + esc + "after"
        reseal(bad)
        human.check(bad)
        text = signer.render(bad, REG)
        assert not any(c in text for c in ("\x1b", "\r", "‮", "\x07"))
        assert "before" in text and "after" in text


# --- 2. the ticket key is a host attestation; the uid fragment is signed ----------------------------------------------


# --- 3. half-applied submits ------------------------------------------------------------------------------------------


def test_an_injected_oserror_on_the_second_append_is_rolled_forward_never_cancelled_or_expired(
    human, served, monkeypatch
):
    _, key = human.ticket()
    with TwoNotes():
        p = human.prepare("log", {"ref": key, "text": "pair"})
    rid = p["request"]["id"]
    signed = human.sign(p)
    store = served.service.store
    real = store.append
    calls = []

    def boom(event, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(event, **kw)

    monkeypatch.setattr(store, "append", boom)
    first = new_idem()
    assert err(human.submit_res(rid, signed.events, idem=first)) == "internal"
    monkeypatch.setattr(store, "append", real)
    assert len(logged(served, key)) == 2  # one of two landed
    # neither cancel nor the clock can end it: its signed events exist
    assert ok(human.c.req("human.cancel", {"request_id": rid}))["data"] == {"state": "submitted"}
    tick(served, 601)
    assert record(served, rid)["state"] == "bound" and record(served, rid)["outcome"] is None
    assert ok(human.get(rid))["data"]["request"]["state"] == "bound"
    other = human.sign(p)  # other signature bytes: not the submission being appended
    assert err(human.submit_res(rid, other.events)) == "human.request_state"
    again = ok(human.submit_res(rid, signed.events))  # the same events, a fresh idem: rolled forward
    assert again["duplicate"] is True and again["seq"] == 3
    assert [e["text"] for e in logged(served, key)[1:]] == ["pair (1)", "pair (2)"]
    assert record(served, rid)["state"] == "submitted"
    assert outcome(served, first)["step"] == "committed"  # the interrupted idem got the same terminal state
    assert ok(human.submit_res(rid, signed.events, idem=first))["seq"] == 3


def test_the_interrupted_idem_itself_rolls_the_submission_forward(human, served, monkeypatch):
    _, key = human.ticket()
    with TwoNotes():
        p = human.prepare("log", {"ref": key, "text": "pair"})
    signed = human.sign(p)
    store = served.service.store
    real = store.append
    calls = []

    def boom(event, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(event, **kw)

    monkeypatch.setattr(store, "append", boom)
    idem = new_idem()
    assert err(human.submit_res(p["request"]["id"], signed.events, idem=idem)) == "internal"
    monkeypatch.setattr(store, "append", real)
    assert ok(human.c.req("outcome.get", {"idem": idem}))["data"]["state"] == "committed"
    assert len(logged(served, key)) == 3


# --- 4. D58 at submit -------------------------------------------------------------------------------------------------


class FakeGit:
    enabled = True
    reader = object()

    def __init__(self, head="a" * 40, identity="local:proj", fail=False):
        self.head_sha, self.ident, self.fail = head, identity, fail

    def ticket_copy(self, root, repos, uid, name):  # H11d: the declared copy (no registration)
        from orch.ops.caps import WorkingCopy

        return WorkingCopy(root.project / name, 0, None, False, None)

    def symbolic_head(self, path):
        return "refs/heads/feat/x"

    def repo_path(self, root, repos, name):
        return root / name

    def head(self, path):
        if self.fail:
            from orch.ops.caps import CapabilityDenied

            raise CapabilityDenied("git")
        return self.head_sha

    def identity(self, path, name):
        return self.ident


def _view(branch="feat/x"):
    return SimpleNamespace(uid="u1", fields={"links": {"repos": ["proj"], "branches": {"proj": branch}}})


def _copies(tmp_path):
    return [FakeGit().ticket_copy(Layout.new_at(tmp_path), {}, "u1", "proj").binding("u1", "proj")]


def _store(tmp_path):
    return SimpleNamespace(
        root=tmp_path,
        layout=Layout.new_at(tmp_path),
        state=SimpleNamespace(workspace=SimpleNamespace(repos={"proj": {}})),
    )


def test_the_source_list_is_reread_through_git_at_submit(tmp_path):
    signed = [{"repo": "local:proj", "ref": "refs/heads/feat/x", "sha": "a" * 40}]
    host_human.recheck_sources(FakeGit(), _store(tmp_path), _view(), signed, _copies(tmp_path))  # unchanged: passes
    with pytest.raises(HostError) as moved:
        host_human.recheck_sources(FakeGit(head="b" * 40), _store(tmp_path), _view(), signed, _copies(tmp_path))
    assert moved.value.code == "gate.stale"
    with pytest.raises(HostError) as branch:
        host_human.recheck_sources(FakeGit(), _store(tmp_path), _view("other"), signed, _copies(tmp_path))
    assert branch.value.code == "gate.stale"
    with pytest.raises(HostError) as unreadable:  # fail closed
        host_human.recheck_sources(FakeGit(fail=True), _store(tmp_path), _view(), signed, _copies(tmp_path))
    assert unreadable.value.code == "observe.unavailable"
    with pytest.raises(HostError) as none:
        host_human.recheck_sources(FakeGit(head=None), _store(tmp_path), _view(), signed, _copies(tmp_path))
    assert none.value.code == "observe.unavailable"


def test_submit_rechecks_every_signed_source_list_under_the_lock(human, served, monkeypatch):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    seen = []

    def judge_all(store, record, events, git):
        seen.append((git.principal, [e["type"] for e in events]))

    monkeypatch.setattr(host_human.HumanOps, "_judge", staticmethod(judge_all))
    ok(human.submit_res(p["request"]["id"], human.sign(p).events))
    assert seen == [(f"uid:{os.getuid()}", ["log.added"])]  # the caller's principal reads git


# --- 5. gate texts; 8. policy and gate_gen ----------------------------------------------------------------------------


def test_gate_material_carries_every_bound_section_text_and_the_policy(human, served):
    _, key = human.ticket()
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    m = p["material"]
    assert set(m["texts"]) == set(m["input"]["sections"]) and m["texts"]["requirements"] == "the requirements"
    assert canon.policy_hash("requirements", m["policy"]) == m["input"]["policy_hash"] == p["events"][0]["policy_hash"]
    text = signer.render(p, REG)
    assert "| the requirements" in text and "| it works" in text
    assert f"gate_gen: {p['events'][0]['gate_gen']} (host attestation" in text and "hash recomputed" in text
    for change, match in (
        (lambda b: b["material"]["texts"].update(requirements="something else"), "does not hash"),
        (lambda b: b["material"]["texts"].pop("context"), "every section"),
        (lambda b: b["material"].pop("policy"), "outside"),  # a required key: the schema check comes first
        (lambda b: b["material"].update(policy="not a policy"), "no policy"),
        (lambda b: b["material"]["policy"].update(count=2), "policy hash"),
    ):
        bad = copy.deepcopy(p)
        change(bad)
        refused(human, reseal(bad), match)


def test_a_bumped_gate_gen_is_displayed_and_the_host_refuses_it_at_submit(human, served):
    _, key = human.ticket()
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    bad = copy.deepcopy(p)
    bad["events"][0]["gate_gen"] += 5
    reseal(bad)
    human.check(bad)  # an attestation: displayed, not recomputed
    assert f"gate_gen: {bad['events'][0]['gate_gen']}" in signer.render(bad, REG)
    assert err(human.submit_res(p["request"]["id"], human.sign(bad).events)) == "human.request_mismatch"


# --- 6. the op is bound -----------------------------------------------------------------------------------------------


def test_the_op_is_bound_by_the_fingerprint_and_by_what_it_may_touch(human, served):
    _, key = human.ticket()
    p = human.prepare("set", {"ref": key, "pairs": ["title=new title"]})
    swapped = copy.deepcopy(p)
    swapped["op"] = "ac.add"
    refused(human, swapped)
    assert review.fingerprint(p["review_digest"], p["events"], "set") != review.fingerprint(
        p["review_digest"], p["events"], "ac.add"
    )
    refused(human, reseal(copy.deepcopy(swapped)), "does not change")
    s = human.prepare("section.set", {"ref": key, "section": "summary", "message": "x"})
    bad = copy.deepcopy(s)
    bad["events"][0]["set"] = {"ticket.title": "sneaky"}
    bad["events"][0]["base_rev"]["ticket.title"] = canon.value_hash("A ticket")
    refused(human, reseal(bad), "does not change")
    bad = copy.deepcopy(p)
    bad["events"][0]["base_rev"]["ticket.labels"] = canon.value_hash([])
    refused(human, reseal(bad), "base_rev")


# --- 7. uploaded artifacts --------------------------------------------------------------------------------------------


def test_the_composite_checks_artifacts_against_the_bytes_it_uploaded(human, served):
    _, key = human.ticket()
    mine = base64.b64encode(b"my evidence\n").decode()

    def run(files, host_files):
        def call(op, args, files=None, idem=None):
            return ok(human.c.req(op, args, files=host_files if op == "human.prepare" else files, idem=idem))

        return signer.run(
            call,
            "artifact.add",
            {"ref": key, "path": "e.txt"},
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=human.person.ref,
            device=human.person.device,
            workspace=REG,
            confirm=lambda text: True,
            show_secret=lambda t: None,
            idem=new_idem(),
            files=files,
        )

    swapped = {"path": {"b64": base64.b64encode(b"the host's bytes\n").decode()}}
    with pytest.raises(signer.SignerRefused, match="not the file you sent"):
        run({"path": {"b64": mine}}, swapped)
    assert human.backend.payloads == []
    assert run({"path": {"b64": mine}}, {"path": {"b64": mine}})["key"] == key


# --- 9. the answer header ---------------------------------------------------------------------------------------------


def test_the_answer_header_shows_the_chosen_option(human, served):
    _, key = human.ticket()
    human.agent("ask", {"ref": key, "text": "deploy?", "options": ["yes", "no"]})
    p = human.prepare("answer", {"ref": key, "question": "Q1", "option": "no"})
    head = signer.header(p, REG)
    assert head[1].endswith("with option no")
    assert "question Q1 (kind choice):" in signer.render(p, REG) and "option yes: yes" in signer.render(p, REG)


# --- Sonnet: device checks, quotas, retention, expiry, question.stale, redactor ---------------------------------------


def _owner_device(lab, scopes, revoke=False):
    ws = lab.ws
    sig_key = crypto.generate_private_key()
    pub = crypto.public_bytes(sig_key)
    cert = certs.make_device_cert(
        ws.owner.pk_pub,
        ws.owner.pk_sign,
        dk_sig_pub=pub,
        dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
        label_sealed=b"x",
        created_ms=ws.clock[0] * 1000,
        expires_ms=None,
        scopes_max=list(scopes),
    )
    ref = certs.device_ref(pub)
    store = ws.other()
    ws.store = store
    try:
        store.append(ws.person_event(ws.owner, "workspace", "device.added", device=ref, cert=cert), log="workspace")
        if revoke:
            ws.tick()
            ws.revoke_device(ref)
    finally:
        store.close()
        ws.store = None
    return ref


def test_device_checks_at_prepare_and_at_binding(human, served, lab):
    _, key = human.ticket()
    member = lab.add_member()
    revoked = _owner_device(lab, ("look", "decide", "operate", "type"), revoke=True)
    no_operate = _owner_device(lab, ("look", "decide"))
    # (a certificate without decide cannot be added at all: F1's scope sets; device.scope is the operate case below)
    cases = [(member.device, "device.unknown"), ("d_" + "0" * 32, "device.unknown"), (revoked, "device.invalid")]
    for device, code in cases:
        signer_ = {"device": device, "auth": "passphrase"}
        assert err(human.prepare_res("log", {"ref": key, "text": "x"}, signer=signer_)) == code, device
        handed = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
        assert err(human.get(handed["request"]["id"], signer=signer_)) == code, device
    no_op = {"device": no_operate, "auth": "passphrase"}
    ok(human.prepare_res("log", {"ref": key, "text": "x"}, signer=no_op))  # decide is enough for a log
    assert err(human.prepare_res("member.remove", {"person": member.ref}, signer=no_op)) == "device.scope"


def test_at_most_20_requests_are_pending_per_person(human, served):
    _, key = human.ticket()
    ids = [human.prepare("log", {"ref": key, "text": f"n{i}"})["request"]["id"] for i in range(MAX_PENDING)]
    human.prepare("log", {"ref": key, "text": "dry"}, dry_run=True)  # a dry run is not counted
    assert err(human.prepare_res("log", {"ref": key, "text": "one too many"})) == "quota.host"
    ok(human.c.req("human.cancel", {"request_id": ids[0]}))
    human.prepare("log", {"ref": key, "text": "fits again"})


def test_terminal_requests_are_kept_24_hours(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    ok(human.c.req("human.cancel", {"request_id": p["request"]["id"]}))
    live = human.prepare("log", {"ref": key, "text": "y"})["request"]["id"]
    tick(served, 23 * 3600)
    served.service.submit(served.service.requests.sweep).result(10)
    assert (requests_dir(served) / p["request"]["id"]).exists()
    tick(served, 3600 + 5)
    served.service.submit(served.service.requests.sweep).result(10)
    assert not (requests_dir(served) / p["request"]["id"]).exists()
    assert json.loads((requests_dir(served) / live).read_text())["state"] == "expired"  # expired, kept 24 h more


def test_cancel_on_an_expired_request_answers_cancelled_and_keeps_it_expired(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    tick(served, 601)
    assert ok(human.c.req("human.cancel", {"request_id": p["request"]["id"]}))["data"] == {"state": "cancelled"}
    assert record(served, p["request"]["id"])["state"] == "expired"


def test_a_grant_request_expires_before_f1s_issued_at_skew(human, served):
    g = human.prepare("grant", {"hours": 1})
    log = human.prepare("log", {"ref": human.ticket()[1], "text": "x"})
    assert GRANT_EXPIRE_SECONDS < 300
    tick(served, GRANT_EXPIRE_SECONDS + 1)
    assert err(human.submit_res(g["request"]["id"], human.sign(g).events)) == "human.request_expired"
    ok(human.submit_res(log["request"]["id"], human.sign(log).events))  # other requests live 10 minutes


def test_a_question_changed_between_prepare_and_submit_is_question_stale(human, served):
    uid, key = human.ticket()
    human.agent("ask", {"ref": key, "text": "deploy?", "options": ["yes", "no"]})
    p = human.prepare("answer", {"ref": key, "question": "Q1", "option": "yes"})
    ws = served.lab.ws
    q = {
        "id": "Q1",
        "to": "ticket_owner",
        "text": "deploy now?",
        "blocking": True,
        "kind": "choice",
        "options": [{"key": "yes", "label": "yes"}, {"key": "no", "label": "no"}],
    }
    qid = canon.question_id(served.wid, uid, "Q1")

    def reask(store):
        store.ticket(uid)
        store.append(
            ws.person_event(
                ws.owner,
                uid,
                "question.asked",
                question=q,
                qid=qid,
                hash=canon.question_hash(qid, uid, q),
            ),
            log=uid,
        )

    served.append(reask)
    assert err(human.submit_res(p["request"]["id"], human.sign(p).events)) == "question.stale"


def test_the_redactor_still_drops_a_non_null_secret_hash():
    r = Redactor()
    out = r.obj({"secret_hash": "sha256:" + "a" * 64, "keep": 1, "nested": [{"secret_hash": "x", "seed": None}]})
    assert out == {"keep": 1, "nested": [{"seed": None}]}
    assert r.obj({"secret_hash": None}) == {"secret_hash": None}


# --- the dashboard's M3 questions (orch-fa) ---------------------------------------------------------------------------


def test_the_fingerprint_test_vector_of_the_doc():
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "log.added",
        "actor": {"kind": "person", "id": "p_" + "1" * 32, "device": "d_" + "2" * 32},
        "auth": "passphrase",
        "hash_v": 1,
        "roster_v": 1,
        "based_on": "sha256:" + "cd" * 32,
        "text": "hello",
        "sig": "x",
    }
    digest = "sha256:" + "ab" * 32
    body = {"op": "log", "review_digest": digest, "events": [review.fingerprint_view(ev)]}
    assert canon.dumps(body).decode() == (
        '{"events":[{"actor":{"id":"p_11111111111111111111111111111111","kind":"person"},'
        '"based_on":"sha256:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd","hash_v":1,'
        '"id":"01J9ZP0000000000000000000B","roster_v":1,"text":"hello","type":"log.added","v":2}],"op":"log",'
        '"review_digest":"sha256:abababababababababababababababababababababababababababababababab"}'
    )
    assert review.fingerprint(digest, [ev], "log") == "d8de03967462"
    assert review.fingerprint(digest, [ev], "set") == "37cf21ef169c"


def test_a_refused_submit_leaves_the_request_bound_and_pending_shows_the_code_only(human, served, lab):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"})
    rid = p["request"]["id"]
    lab.add_member()  # roster moves: members.stale
    assert err(human.submit_res(rid, human.sign(p).events)) == "members.stale"
    assert record(served, rid)["state"] == "bound"  # not terminal: it expires after 10 minutes
    (item,) = ok(human.c.req("human.pending", {"request_id": rid}))["data"]["requests"]
    assert item["state"] == "bound" and item["error"] == {"code": "members.stale"} and item["seq"] is None
    assert item["key"] == key and item["event_ids"] == [p["events"][0]["id"]] and "code" not in item
    assert set(item["error"]) == {"code"}
    done = human.prepare("log", {"ref": key, "text": "y"})
    res = ok(human.submit_res(done["request"]["id"], human.sign(done).events))
    listed = {r["id"]: r for r in ok(human.c.req("human.pending", {}))["data"]["requests"]}
    assert listed[done["request"]["id"]]["seq"] == res["seq"] and "error" not in listed[done["request"]["id"]]
    assert ok(human.c.req("human.pending", {"request_id": "hr_" + "0" * 26}))["data"] == {"requests": []}
    assert err(human.c.req("human.pending", {"code": "ABCD-EFGH"})) == "invalid.input"


def test_a_material_key_outside_the_schema_is_refused(human, served):
    _, key = human.ticket()
    ready(human, key)
    human.agent("ask", {"ref": key, "text": "q?", "options": ["a", "b"]})
    for op, args in (
        ("log", {"ref": key, "text": "x"}),
        ("approve", {"ref": key, "gate": "requirements"}),
        ("answer", {"ref": key, "question": "Q1", "option": "a"}),
    ):
        p = human.prepare(op, args)
        human.check(p)
        bad = copy.deepcopy(p)
        bad["material"]["note"] = "SAFE: nothing changes"
        refused(human, reseal(bad), "outside")
        if op == "approve":
            bad = copy.deepcopy(p)
            bad["material"].pop("texts")
            refused(human, reseal(bad), "outside")


def test_the_expiry_error_names_the_requests_lifetime(human, served):
    _, key = human.ticket()
    g = human.prepare("grant", {"hours": 1})
    p = human.prepare("log", {"ref": key, "text": "x"})
    tick(served, 601)
    for prepared, minutes in ((g, 4), (p, 10)):
        res = human.submit_res(prepared["request"]["id"], human.sign(prepared).events)
        assert err(res) == "human.request_expired"
        assert f"it lives {minutes} minutes" in res["error"]["message"]


def test_recheck_sources_runs_under_the_store_lock(human, served, monkeypatch):
    import contextlib

    _, key = human.ticket()
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    requests = served.service.requests
    rec = requests.read(p["request"]["id"])
    rec["prepared"]["events"][0]["source_sha"] = []  # as a code approval or a verdict would sign it
    requests.write(rec)
    store = served.service.store
    depth = [0]
    real_locked = store.locked

    @contextlib.contextmanager
    def counted():
        with real_locked():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    seen = []

    def recheck(git, st, view, signed, copies=None):
        seen.append((depth[0], st is store, git.principal, signed))
        raise HostError("gate.stale", "moved")

    monkeypatch.setattr(store, "locked", counted)
    monkeypatch.setattr(host_human, "recheck_sources", recheck)
    events = [dict(rec["prepared"]["events"][0])]
    from orch.identity import sign_person_event

    events[0]["sig"] = sign_person_event(human.backend, "dk", served.wid, p["log"], events[0], action="t")
    assert err(human.submit_res(p["request"]["id"], events)) == "gate.stale"
    assert seen == [(1, True, f"uid:{os.getuid()}", [])]  # inside store.locked(), as the caller
