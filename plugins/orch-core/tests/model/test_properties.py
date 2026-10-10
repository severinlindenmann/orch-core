"""Property tests: random event sequences never reach an impossible state, and admit and replay agree."""

import copy
import pickle
import random
from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orch import canon
from orch.model import admit, advance, at, replay
from tests.model.world import SHA1, SHA2, SHA3, World, pol, stamp, ulid
from tests.schema.examples import digest, revocation, sig

SHAS = (SHA1, SHA2, SHA3)
TEXTS = ("alpha", "beta", "gamma")
GATES = ("requirements", "plan", "verify", "code")
SESSIONS = ("s_" + ulid(1), "s_" + ulid(1) + ".1", "s_" + ulid(2))


class Pick:
    """Draws from a fixed list of small ints (so Hypothesis can shrink a failing walk step by step)."""

    def __init__(self, ints):
        self.ints = list(ints)

    def _next(self):
        return self.ints.pop(0) if self.ints else 0

    def randrange(self, n):
        return self._next() % n

    def choice(self, seq):
        return seq[self._next() % len(seq)]

    def random(self):
        return (self._next() % 100) / 100


def make_world():
    w = World().bootstrap({"mara": "maintainer", "tom": "member", "vera": "viewer"})
    w.wev(
        "policy.changed",
        "sev",
        gates={
            "requirements": pol(["owner", "maintainer"], 1),
            "plan": pol(["owner", "maintainer"], 1),
            "verify": pol(["owner", "maintainer", "member"], 1),
            "code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True),
        },
    )
    w.settings(repos={"dbt": {"path": "../dbt"}})
    grants = {n: w.grant(n, "all" if n != "tom" else "workable") for n in ("sev", "mara", "tom")}
    uids = []
    for _ in range(2):
        uid = w.ticket()
        w.fill(uid)
        w.edit(
            uid, "sev", {"ticket.links": {"repos": ["dbt"], "branches": {"dbt": "feat/x"}, "prs": [], "external": []}}
        )
        w.push(uid)
        uids.append(uid)
    return w, uids, grants


def projection(v):
    return (
        v.status,
        tuple((g, gv.gen, gv.counting, gv.hash) for g, gv in v.gates.items()),
        v.claim.session if v.claim else None,
        tuple((t.id, t.state, t.holder) for t in v.tasks),
        tuple(a.name for a in v.artifacts),
        tuple(tuple(x.items()) for x in v.source_list),
        tuple((q.id, q.answered) for q in v.questions),
        tuple(v.people.items()),
    )


def decision(w, v, who, gate, kind="approve"):
    gv = v.gates[gate]
    p = {"gate_gen": gv.gen, "hash": gv.hash, "policy_hash": gv.policy_hash}
    sl = [dict(x) for x in v.source_list]
    if gate == "verify":
        p.update(outcome="pass" if kind == "approve" else "fail", source_sha=sl)
        if kind != "approve":
            p["text"] = "t"
        return "verdict.given", who, p
    if kind == "approve":
        p["gate"] = gate
        if gate == "code":
            p["source_sha"] = sl
        return "gate.approved", who, p
    p.update(gate=gate, text="t")
    return "gate.changes_requested", who, p


def progress(w, rnd, uid, v, grants):
    """A move that usually succeeds, so the walk reaches testing and done."""
    names = {p: n for n, p in w.people.items()}
    reviewer = rnd.choice(["sev", "mara"])
    if v.status in ("open", "backlog"):
        if rnd.random() < 0.5:
            return uid, *decision(w, v, reviewer, rnd.choice(["requirements", "plan"]))
        return uid, "claim.taken", w.agent("sev", grants["sev"], SESSIONS[0]), {}
    if v.status == "in_progress":
        c = v.claim
        sess = c.session if c else SESSIONS[0]
        who = names.get(c.for_person, "sev") if c else "sev"
        agent = w.agent(who, grants[who], sess)
        tasks = v.fields["tasks"]
        undone = [t for t in v.tasks if t.state != "done"]
        if rnd.random() < 0.15:
            return uid, *decision(w, v, reviewer, rnd.choice(["requirements", "plan"]), "changes")
        if not v.gates["requirements"].approved:
            return uid, *decision(w, v, reviewer, "requirements")
        if not v.gates["plan"].approved:
            return uid, *decision(w, v, reviewer, "plan")
        if undone and tasks and tasks[0]["verify"]:
            cmd = tasks[0]["verify"]["cmd"]
            sha = v.source_list[0]["sha"] if v.source_list else SHA1
            return (
                uid,
                "task.done",
                agent,
                {"task": tasks[0]["id"], "receipt": {"cmd": cmd, "exit": 0, "ms": 1, "repo": "dbt", "commit": sha}},
            )
        return uid, "ticket.submitted", agent, {}
    if v.status == "testing":
        gate = "verify" if not v.gates["verify"].approved else "code"
        return uid, *decision(w, v, reviewer, gate, "approve" if rnd.random() < 0.85 else "changes")
    return uid, rnd.choice(["ticket.reopened", "log.added"]), "sev", {}


def act(w, rnd, uids, grants, st_):
    uid = rnd.choice(uids)
    v = st_.tickets[uid]
    if rnd.random() < 0.65:
        return progress(w, rnd, uid, v, grants)
    person = rnd.choice(["sev", "mara", "tom", "vera"])
    sess = rnd.choice(SESSIONS)
    agent_name = rnd.choice(["sev", "mara", "tom"])
    agent = w.agent(agent_name, grants[agent_name], sess)
    kind = rnd.randrange(25)
    if kind == 15 and rnd.random() < 0.8:
        kind = 21
    if kind == 0:
        cur = v.claim.session if v.claim else SESSIONS[0]
        return (
            uid,
            "claim.taken",
            agent,
            ({"takeover": {"from_session": cur, "reason": "r"}} if rnd.random() < 0.5 else {}),
        )
    if kind == 1 and v.claim:
        who = agent if rnd.random() < 0.5 else person
        if who is agent:
            agent = {**agent, "session": v.claim.session}
            return (
                uid,
                "claim.released",
                agent,
                {"session": v.claim.session, "reason": rnd.choice(["released", "handoff"])},
            )
        return uid, "claim.released", person, {"session": v.claim.session, "reason": "released"}
    if kind in (2, 3):
        sec = rnd.choice(["context", "requirements", "out_of_scope", "plan", "decisions", "verification"])
        text = rnd.choice(TEXTS)
        base = v.sections[sec]["hash"] if sec in v.sections else canon.section_hash("")
        actor = agent if rnd.random() < 0.5 else person
        return (
            uid,
            "ticket.updated",
            actor,
            {"base_rev": {"body." + sec: base}, "sections": {sec: {"hash": canon.section_hash(text), "refs": []}}},
        )
    if kind == 4:
        tasks = rnd.choice(
            [
                [],
                [{"id": "T1", "text": "a", "verify": {"cmd": "make a"}, "proves": ["AC1"]}],
                [
                    {"id": "T1", "text": "a", "verify": {"cmd": "make a"}, "proves": ["AC1"]},
                    {"id": "T2", "text": "b", "verify": None, "proves": []},
                ],
            ]
        )
        cur = _thaw(v.fields["tasks"])
        return (
            uid,
            "ticket.updated",
            agent if rnd.random() < 0.5 else person,
            {"base_rev": {"ticket.tasks": canon.value_hash(cur)}, "set": {"ticket.tasks": tasks}},
        )
    if kind == 5:
        typ = rnd.choice(["task.started", "task.done", "task.skipped", "task.blocked", "task.reopened"])
        p = {"task": rnd.choice(["T1", "T2"])}
        if typ == "task.done" and rnd.random() < 0.8:
            p["receipt"] = {"cmd": "make a", "exit": 0, "ms": 1, "repo": "dbt", "commit": rnd.choice(SHAS)}
            p["task"] = "T1"
        if typ in ("task.skipped", "task.blocked"):
            p["reason"] = "r"
        return uid, typ, agent, p
    if kind == 6:
        return uid, "ticket.submitted", agent, {}
    if kind in (7, 8, 9):
        gate = rnd.choice(GATES)
        gv = v.gates[gate]
        gen = gv.gen if rnd.random() < 0.9 else gv.gen - 1
        p = {"gate_gen": max(gen, 0), "hash": gv.hash, "policy_hash": gv.policy_hash}
        sl = [dict(x) for x in v.source_list]
        k = rnd.choice(["approve", "approve", "changes"])
        if gate == "verify":
            p.update(outcome="pass" if k == "approve" else "fail", source_sha=sl)
            if k != "approve":
                p["text"] = "t"
            return uid, "verdict.given", person, p
        if k == "approve":
            p["gate"] = gate
            if gate == "code":
                p["source_sha"] = sl
            return uid, "gate.approved", person, p
        p.update(gate=gate, text="t")
        return uid, "gate.changes_requested", person, p
    if kind == 10:
        role = rnd.choice(["assignees", "reviewers", "watchers"])
        tgt = [w.people[rnd.choice(["sev", "mara", "tom"])]]
        add, rem = (tgt, []) if rnd.random() < 0.6 else ([], tgt)
        return uid, "people.changed", "sev", {"role": role, "add": add, "remove": rem}
    if kind == 11:
        g = rnd.choice(["requirements", "plan", "verify"])
        return uid, "policy.changed", "sev", {"gates": {g: pol(["owner", "maintainer", "member"], rnd.choice([1, 2]))}}
    if kind == 12:
        r = rnd.choice(SHAS)
        cur = None
        for e in reversed(w.tl[uid]):
            if e["type"] == "branch.pushed":
                cur = {"repo_id": e["repo_id"], "ref": e["ref"], "sha": e["sha"]}
                break
        return (
            uid,
            "branch.pushed",
            w.HOST,
            {
                "repo_name": "dbt",
                "repo_id": "https://github.com/acme/energy-dbt",
                "ref": "refs/heads/feat/x",
                "sha": r,
                "before": cur,
            },
        )
    if kind == 13:
        n = rnd.choice(["a.log", "b.log"])
        actor = agent if rnd.random() < 0.7 else w.unattended()
        p = {"name": n, "kind": "log", "sha256": digest(n + str(rnd.random())), "bytes": 1}
        if actor["kind"] == "agent" and not actor.get("unattended") and rnd.random() < 0.5:
            p["ac"] = "AC1"
        return uid, "artifact.added", actor, p
    if kind == 14:
        return (
            uid,
            rnd.choice(["ticket.closed", "ticket.reopened"]),
            "sev",
            {"resolution": "other"} if rnd.random() < 0.5 else {},
        )
    if kind == 15:
        who = rnd.choice(["mara", "tom"])
        typ = rnd.choice(["member.removed", "role.changed", "device.revoked"])
        if typ == "member.removed":
            return "workspace", typ, "sev", {"person": w.people[who]}
        if typ == "role.changed":
            return "workspace", typ, "sev", {"person": w.people[who], "role": rnd.choice(["member", "maintainer"])}
        rev = revocation(w.people[who][2:], w.dev[who][2:], "compromised")
        return "workspace", typ, w.HOST, {"device": w.dev[who], "reason": "compromised", "revocation": rev}
    if kind == 16:
        w.clock += __import__("datetime").timedelta(minutes=rnd.choice([1, 30, 90, 200]))
        return uid, "log.added", agent, {"text": "tick"}
    if kind == 17:
        q = {"id": "Q1", "to": w.people["tom"], "text": rnd.choice(TEXTS), "blocking": True}
        qid = canon.question_id(w.workspace_id, uid, "Q1")
        return (
            uid,
            "question.asked",
            agent,
            {"question": q, "qid": qid, "hash": canon.question_hash(qid, uid, q["text"], None)},
        )
    if kind == 18 and v.questions:
        return uid, "question.answered", person, {"question": "Q1", "hash": v.questions[0].hash, "text": "ok"}
    if kind == 19:
        return (
            uid,
            "edit.external",
            w.HOST,
            {
                "sections": {"context": {"hash": canon.section_hash("ext"), "refs": []}},
                "voided_gates": rnd.choice([[], ["requirements"]]),
                "normalised": False,
            },
        )
    if kind == 20:
        return (
            uid,
            "ticket.updated",
            agent,
            {
                "base_rev": {"ticket.size": canon.value_hash(v.fields["size"])},
                "set": {"ticket.size": rnd.choice(["s", "m", None])},
            },
        )
    if kind == 22 and len(uids) > 1:  # a reference to another ticket at the very same `at` (merged-order tie)
        other = rnd.choice([u for u in uids if u != uid])
        key = st_.tickets[other].key
        return (
            uid,
            "ticket.updated",
            "sev",
            {
                "base_rev": {"ticket.parent": canon.value_hash(v.fields["parent"])},
                "set": {"ticket.parent": key},
                "at": w.at(),
            },
        )
    if kind == 23:
        w.uid_n += 1
        new = "01J9ZK4Q7M3R8T2V6X0B" + f"{w.uid_n:06d}"
        return (
            new,
            "ticket.created",
            "sev",
            {
                "key": f"DEMO-{w.uid_n:04d}",
                "ticket_type": "feature",
                "title": "n",
                "owner": w.people["sev"],
                "at": w.at(),
            },
        )
    if kind == 24:  # a key that is taken
        return (
            "01J9ZK4Q7M3R8T2V6X0B" + f"{w.uid_n + 50:06d}",
            "ticket.created",
            "sev",
            {"key": st_.tickets[uid].key, "ticket_type": "bug", "title": "dup", "owner": w.people["sev"]},
        )
    return uid, "log.added", person, {"text": "hello"}


def _thaw(o):
    if hasattr(o, "items"):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, tuple | list):
        return [_thaw(v) for v in o]
    return o


def claim_oracle(events):
    """Two live claims never exist: an independent walk over the accepted claim events."""
    holder = None
    for e in events:
        if e["type"] == "claim.taken":
            assert holder is None or ("takeover" in e and e["takeover"]["from_session"] == holder), "two claims"
            holder = e["actor"]["session"]
        elif e["type"] == "claim.released" and e["session"] == holder:
            holder = None
        elif e["type"] in ("ticket.closed", "ticket.reopened"):
            holder = None  # a closed or done ticket's claim has ended (§5.12); reopen starts clean


def check(w, s):
    assert not s.chain_errors and not s.workspace.invalid, (s.chain_errors, s.workspace.invalid)
    for uid, v in s.tickets.items():
        assert not v.frozen
        claim_oracle(w.tl[uid])
        for g, gv in v.gates.items():
            assert gv.approved == (len(gv.counting) >= gv.needed and gv.applies)
            for d in gv.decisions:
                if not d.counting:
                    continue
                assert d.gen == gv.gen  # an approval never survives a generation change
                assert d.hash == gv.hash and d.policy_hash == gv.policy_hash  # nor a changed hash
                if g in ("verify", "code"):
                    assert [dict(x) for x in d.source_sha] == [dict(x) for x in v.source_list]  # nor another commit
        for t in v.tasks:
            if t.state == "done":
                assert t.holder is not None  # no done task without a lease holder
        assignees = set()
        for e in w.tl[uid]:
            if e["type"] == "people.changed" and e["role"] == "assignees":
                assignees = (assignees | set(e["add"])) - set(e["remove"])
            if e["type"] == "gate.approved" and e["gate"] == "code":
                assert e["actor"]["id"] not in assignees  # no code approval by an assignee


def assert_same(a, b, note):
    assert a.workspace == b.workspace, note
    assert a.tickets.keys() == b.tickets.keys(), note
    for uid in a.tickets:
        assert a.tickets[uid] == b.tickets[uid], (note, uid)
    assert a.chain_errors == b.chain_errors, note


def oracle_workers(w, uid):
    """§5.7 workers at each accepted decision, from the raw log alone: independent approvals by a worker never count."""
    workers, holder, bad = set(), None, []
    ws_indep = {g: False for g in GATES}
    ws_indep["code"] = True
    override = {g: False for g in GATES}
    for e in w.tl[uid]:
        t, a = e["type"], e["actor"]
        if t == "ticket.reopened":
            workers, holder = set(), None
        elif t == "people.changed" and e["role"] == "assignees":
            workers |= set(e["add"])
        elif t == "claim.taken":
            holder = a["for"]
        elif t == "claim.released":
            holder = None
        elif t == "policy.changed":
            for g, p in e["gates"].items():
                override[g] = override[g] or p["independent"]
        if a["kind"] == "agent" and "for" in a:
            workers.add(a["for"])
        if a["kind"] == "host" and t == "branch.pushed" and holder:
            workers.add(holder)
        if t in ("gate.approved", "verdict.given") and (
            t == "verdict.given" and e["outcome"] == "pass" or t == "gate.approved"
        ):
            g = "verify" if t == "verdict.given" else e["gate"]
            if (ws_indep[g] or override[g]) and a["id"] in workers:
                bad.append((g, a["id"], e["id"]))
    return bad


def run(steps):
    w, uids, grants = make_world()
    adv = st_ = w.state()
    history = []
    try:
        for step in steps:
            rnd = Pick(step)
            log, typ, actor, payload = act(w, rnd, uids, grants, st_)
            history.append(typ)
            if isinstance(actor, str) and actor not in w.people:
                continue
            before_ws, before_tl = len(w.ws), {u: len(x) for u, x in w.tl.items()}
            prev, pickled = adv, pickle.dumps(adv._core)
            try:
                e, res = w.try_(log, typ, actor, **copy.deepcopy(payload))
            except Exception as err:  # a generated event the schema refuses is not a model matter
                if err.__class__.__name__ == "SchemaError":
                    continue
                raise
            if hasattr(res, "code"):
                assert len(w.ws) == before_ws and {u: len(x) for u, x in w.tl.items()} == before_tl
                if res.code.value.startswith(("chain.", "event.unknown")):
                    continue
                # refused by admit: appended anyway it must replay as absent, with the same code
                ws2, tl2 = copy.deepcopy(w.ws), copy.deepcopy(w.tl)
                e["host_sig"] = sig("host-refused")
                (ws2 if log == "workspace" else tl2.setdefault(log, [])).append(e)
                s2 = replay(ws2, tl2, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)
                hit = [i for i in s2._core.logs[log].invalid if i.id == e["id"]]
                assert hit and hit[0].code == res.code.value, (typ, res)
                for uid in uids:
                    assert projection(s2.tickets[uid]) == projection(st_.tickets[uid]), (typ, res)
                # advance of a refused event == replay of it
                forged = advance(adv, e, log=log, now=w.at())
                assert_same(forged, s2, ("refused advance", typ))
            else:
                if typ == "ticket.created":
                    uids.append(log)
                adv = advance(adv, e, log=log, now=w.at())
                assert_same(adv, w.state(), ("advance", typ))
            st_ = w.state()
            later = at(prev, stamp(w.clock + timedelta(minutes=45)))
            admit(prev, e, log=log)
            assert pickle.dumps(prev._core) == pickled, "admit/advance/at changed the earlier State"
            assert_same(at(st_, st_.now), st_, ("at", typ))
            assert later.now > st_.now and at(later, st_.now).now == st_.now
            check(w, st_)
            for uid in uids:
                assert oracle_workers(w, uid) == [], (uid, oracle_workers(w, uid))
    except Exception as err:
        err.add_note("sequence: " + " ".join(history))
        raise
    return w


STEP = st.lists(st.integers(0, 99), min_size=14, max_size=14)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(STEP, min_size=5, max_size=30))
def test_random_sequences_keep_every_invariant_and_admit_agrees_with_replay(steps):
    run(steps)


def test_a_long_fixed_sequence():
    rnd = random.Random(7)
    w = run([[rnd.randrange(100) for _ in range(14)] for _ in range(60)])
    assert w.state().tickets  # smoke: the walk produced a log that replays
