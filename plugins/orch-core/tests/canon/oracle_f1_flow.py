"""Independent oracle, part 5: ticket flows (generations §5.7, status §5.9, effective policy, source list, questions).

:class:`Sim` drives a :class:`~oracle_f1_world.World` and keeps its *own* model of the ticket: the §5.7 raise table with
the one-raise-per-event rule and the cascade, the counting approvals, the §5.9 status table and the gate hash input
(through :mod:`oracle_f1_gate`). Every accepted step records the state the model must reach (``after``: generations,
status, approved gates, counting persons, the four gate hashes), so a scenario file is a literal known answer that
``orch.model`` has to reproduce. Refusals are declared by the scenario author. Nothing here imports ``orch``.
"""

# ruff: noqa: E501
from __future__ import annotations

import copy
import hashlib
from typing import Any

from . import oracle_f1_gate as g
from . import oracle_f1_world as ow
from .oracle_f1_world import TICKET, World, did, pid

GATES = g.GATES
REQ_PATHS = {
    "ticket.type", "ticket.size", "ticket.acceptance", "body.summary", "body.context", "body.requirements",
    "body.out_of_scope",
}  # fmt: skip
BOUND = {
    "requirements": REQ_PATHS,
    "plan": REQ_PATHS | {"ticket.tasks", "body.plan", "body.decisions"},
    "verify": {"ticket.type", "ticket.size", "ticket.acceptance", "ticket.links", "body.verification", "body.findings"},
    "code": {"ticket.type", "ticket.size", "ticket.acceptance", "ticket.links"},
}
SESSION = "s_01J9ZK00000000000000000S01"
REPO = "acme-energy-dbt"
REMOTE = "git@github.com:acme/energy-dbt.git"
SHA_A, SHA_B, SHA_C = "a1" * 20, "b2" * 20, "c3" * 20


def vh(value: Any) -> str:
    return "sha256:" + hashlib.sha256(b"orch/v2/value|" + ow.cj(value)).hexdigest()


def policy(approvers, count=1, not_=(), applies="all", independent=False) -> dict[str, Any]:
    return {
        "approvers": sorted(approvers), "count": count, "not": sorted(not_), "applies": applies,
        "independent": independent,
    }  # fmt: skip


CODE_ON = policy(["maintainer", "owner"], 1, ["assignees"], "all", True)


class Sim:
    """One workspace and one ticket (``TICKET``, owned by sev), with the oracle's own idea of what must follow."""

    def __init__(self, name: str, pins: str, ty: str = "feature") -> None:
        self.w = World(name, pins)
        w = self.w
        w.genesis_event()
        w.member("mara", "maintainer")
        w.member("lena", "member")
        w.ev(
            "workspace", "settings.changed", w.actor("sev"), {"set": {"repos": {REPO: {"path": "../acme-energy-dbt"}}}}
        )
        now = w.tick()
        self.grant = "gr_01J9ZK00000000000000000G01"
        w.ev(
            "workspace", "grant.issued", w.actor("sev"),
            {"grant": self.grant, "scope": "all", "verbs": "agent", "issued_at": now, "hours": 8,
             "expires_at": ow.stamp(w.clock + 8 * 3600), "secret_hash": g.digest(b"secret")},
            at=now,
        )  # fmt: skip
        self.t: dict[str, Any] = {
            "workspace_id": ow.W, "uid": TICKET, "type": ty, "size": None, "owner": pid("sev"),
            "people": {"assignees": [], "reviewers": [], "watchers": []}, "sections": {}, "acceptance": [],
            "tasks": [], "links": {"repos": [], "branches": {}, "prs": [], "external": []}, "remotes": {REPO: REMOTE},
            "heads": {}, "artifacts": {}, "receipts": {}, "addons": {}, "addon_values": {},
            "ws_policies": copy.deepcopy(g.DEFAULT_POLICIES), "overrides": {}, "prior": {},
        }  # fmt: skip
        self.gen = dict.fromkeys(GATES, 0)
        self.last_raised: list[str] = []
        self.full_policy = False
        self.dec: dict[str, list[dict[str, Any]]] = {x: [] for x in GATES}
        self.status = "open"
        self.tasks: dict[str, str] = {}
        self.fields_extra: dict[str, Any] = {"title": "A ticket"}
        self.created = False
        self.agent = w.agent("sev", self.grant, SESSION)
        w.ticket(TICKET)
        self.created = True
        self._record_after()

    # -- the oracle's model ----------------------------------------------------------------------------------
    def pol(self) -> dict[str, dict[str, Any]]:
        return g.derive_policies(self.t)

    def applies(self, gate: str) -> bool:
        return g.applies_to(self.pol()[gate], self.t["type"])

    def counting(self, gate: str) -> list[dict[str, Any]]:
        out, seen = [], set()
        for d in self.dec[gate]:
            if (
                d["kind"] in ("approve", "pass")
                and d["gen"] == self.gen[gate]
                and not d["voided"]
                and d["person"] not in seen
            ):
                seen.add(d["person"])
                out.append(d)
        return out

    def first(self, gate: str) -> list[str]:
        return sorted(d["id"] for d in self.counting(gate)[: self.pol()[gate]["count"]])

    def reached(self, gate: str) -> bool:
        return len(self.counting(gate)) >= self.pol()[gate]["count"]

    def approved(self, gate: str) -> bool:
        return self.applies(gate) and self.reached(gate)

    def done_rule(self) -> bool:
        needed = [x for x in ("verify", "code") if self.applies(x)]
        return bool(needed) and all(self.reached(x) for x in needed)

    def snap(self) -> dict[str, Any]:
        return {x: {"applies": self.applies(x), "first": self.first(x)} for x in GATES}

    def settle(self, before: dict[str, Any], marks: set[str]) -> list[str]:
        def app(x: str) -> bool:
            return before[x]["applies"] or self.applies(x)

        direct = {x for x in marks if app(x)}
        changed = {x for x in GATES if self.first(x) != before[x]["first"]}
        triggers = [GATES.index(x) for x in direct | changed]
        raised = set(direct)
        if triggers:
            m = min(triggers)
            raised |= {x for i, x in enumerate(GATES) if i > m and app(x)}
        for x in raised:
            self.gen[x] += 1
        self.last_raised = [x for x in GATES if x in raised]
        return self.last_raised

    def bound(self, gate: str) -> set[str]:
        paths = set(BOUND[gate])
        for name, a in self.t["addons"].items():
            paths |= {f"ticket.addons.{name}.{f}" for f, gs in a["binds"]["fields"].items() if gate in gs}
            paths |= {
                "body." + s["id"] for s in a["binds"]["sections"] if gate in s["gate"] and self.t["type"] in s["types"]
            }
        return paths

    def sync(self) -> None:
        self.t["prior"] = {x: {"gen": self.gen[x], "counting": [d["id"] for d in self.counting(x)]} for x in GATES}

    def G(self, gate: str) -> dict[str, Any]:
        self.sync()
        return g.derive_G(self.t, gate)

    def hash(self, gate: str) -> str:
        return g.gate_hash_of(self.G(gate))

    def phash(self, gate: str) -> str:
        p = self.pol()[gate]
        return g.policy_hash(gate, p if p["approvers"] else {**p, "approvers": ["owner"]})

    def source(self) -> list[dict[str, str]]:
        return g.source_list(self.t)

    # -- recording -------------------------------------------------------------------------------------------
    def expected(self) -> dict[str, Any]:
        t: dict[str, Any] = {
            "gens": [self.gen[x] for x in GATES], "status": self.status,
            "approved": [x for x in GATES if self.approved(x)],
            "counting": {x: sorted(d["person"] for d in self.counting(x)) for x in GATES if self.counting(x)},
            "hash": {x: self.hash(x) for x in GATES if self.pol()[x]["approvers"]},
        }  # fmt: skip
        if self.source():
            t["source_list"] = self.source()
        if self.full_policy:  # the effective policy is pinned in the scenarios that are about it
            t["policy"] = {x: self.pol()[x] for x in GATES}
            t["policy_hash"] = {x: self.phash(x) for x in GATES if self.pol()[x]["approvers"]}
            t["blocked"] = [x for x in GATES if not self.pol()[x]["approvers"]]
        return {"tickets": {TICKET: t}}

    def _record_after(self) -> None:
        if self.created and self.w.steps and self.w.steps[-1]["expect"] == "ok":
            self.w.steps[-1]["after"] = {**self.w.steps[-1].get("after", {}), **self.expected()}

    def _t(
        self, typ: str, actor: dict[str, Any], payload: dict[str, Any], expect: str = "ok", **kw: Any
    ) -> dict[str, Any]:
        self.last_raised = []
        return self.w.ev(TICKET, typ, actor, payload, expect=expect, **kw)

    def _fin(self, expect: str) -> None:
        if expect == "ok":
            self._record_after()

    def _sev(self) -> dict[str, Any]:
        return self.w.actor("sev")

    # -- ticket events --------------------------------------------------------------------------------------------
    def cur(self, path: str) -> Any:
        key = path[len("ticket.") :]
        if key.startswith("addons."):
            _, a, f = key.split(".")
            return self.t["addon_values"].get(a, {}).get(f)
        return {"title": self.fields_extra["title"], "size": self.t["size"], "acceptance": self.t["acceptance"],
                "tasks": self.t["tasks"], "links": self.t["links"], "type": self.t["type"]}[key]  # fmt: skip

    def edit(self, sets: dict[str, Any] | None = None, sections: dict[str, str | None] | None = None, *,
             who: str | dict[str, Any] = "sev", expect: str = "ok", note: str | None = None, **kw: Any) -> dict[str, Any]:  # fmt: skip
        sets, sections = sets or {}, sections or {}
        base = {p: vh(self.cur(p)) for p in sets}
        base |= {"body." + s: g.section_h(self.t["sections"].get(s, "")) for s in sections}
        payload: dict[str, Any] = {"base_rev": base}
        if sets:
            payload["set"] = copy.deepcopy(sets)
        if sections:
            payload["sections"] = {
                s: None if x is None else {"hash": g.section_h(x), "refs": g.refs_of(x)} for s, x in sections.items()
            }
        actor = self.w.actor(who) if isinstance(who, str) else who
        before = self.snap()
        e = self._t("ticket.updated", actor, payload, expect, note=note, **kw)
        if expect == "ok":
            for p, v in sets.items():
                key = p[len("ticket.") :]
                if key.startswith("addons."):
                    _, a, f = key.split(".")
                    self.t["addon_values"].setdefault(a, {})[f] = copy.deepcopy(v)
                elif key == "title":
                    self.fields_extra["title"] = v
                else:
                    self.t[key] = copy.deepcopy(v)
            for s, x in sections.items():
                if x is None:
                    self.t["sections"].pop(s, None)
                else:
                    self.t["sections"][s] = x
            paths = set(sets) | {"body." + s for s in sections}
            self.settle(before, {x for x in GATES if self.bound(x) & paths})
        self._fin(expect)
        return e

    def ready(self) -> dict[str, Any]:
        """Everything the requirements and plan gates need, in one edit (so every gate is raised once)."""
        return self.edit(
            {"ticket.size": "m", "ticket.acceptance": [{"id": "AC1", "text": "it works"}],
             "ticket.tasks": [{"id": "T1", "text": "do it", "verify": {"cmd": "echo ok"}, "proves": ["AC1"]}]},
            {"context": "ctx", "requirements": "req", "out_of_scope": "nothing", "plan": "1. do it", "decisions": "d1",
             "verification": "ran it"},
            note="the ticket is filled in one edit",
        )  # fmt: skip

    def decide(self, kind: str, gate: str, who: str = "sev", *, expect: str = "ok", note: str | None = None,
               gen: int | None = None, text: str | None = None, stale: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:  # fmt: skip
        """``kind``: approve | changes | pass | fail. The signed hash, policy hash, generation and source list are the
        current ones, unless ``gen`` or ``stale`` override them."""
        pay: dict[str, Any] = {"gate_gen": self.gen[gate] if gen is None else gen, "hash": self.hash(gate),
                               "policy_hash": self.phash(gate)}  # fmt: skip
        if kind == "pass" or kind == "fail":
            typ = "verdict.given"
            pay["outcome"] = kind
            pay["source_sha"] = self.source()
        elif kind == "changes":
            typ = "gate.changes_requested"
            pay |= {"gate": gate, "text": text or "needs work"}
        else:
            typ = "gate.approved"
            pay["gate"] = gate
            if gate == "code":
                pay["source_sha"] = self.source()
        if kind == "fail":
            pay["text"] = text or "does not work"
        pay |= stale or {}
        before = self.snap()
        e = self._t(typ, self.w.actor(who), pay, expect, note=note, **kw)
        if expect == "ok":
            self.dec[gate].append({"id": e["id"], "person": pid(who), "device": e["actor"]["device"], "kind": kind,
                                   "gen": pay["gate_gen"], "voided": False})  # fmt: skip
            marks: set[str] = set()
            if kind == "changes":
                marks = set(GATES[GATES.index(gate) :])
                if self.status == "testing":
                    self.status = "in_progress"
            elif kind == "fail":
                marks = set(GATES[GATES.index("verify") :])
                self.status = "in_progress"
            self.settle(before, marks)
            if kind in ("approve", "pass") and self.status == "testing" and self.done_rule():
                self.status = "done"
        self._fin(expect)
        return e

    def people(self, role: str, add: list[str], remove: list[str] | None = None, *, expect: str = "ok",
               note: str | None = None, who: str = "sev") -> dict[str, Any]:  # fmt: skip
        before = self.snap()
        e = self._t("people.changed", self.w.actor(who), {"role": role, "add": [pid(p) for p in add],
                    "remove": [pid(p) for p in (remove or [])]}, expect, note=note)  # fmt: skip
        if expect == "ok":
            token = "ticket_owner" if role == "owner" else role
            if role == "owner":
                self.t["owner"] = pid(add[0])
            else:
                cur = set(self.t["people"][role]) | {pid(p) for p in add}
                self.t["people"][role] = sorted(cur - {pid(p) for p in (remove or [])})
            self.settle(before, {x for x in GATES if token in g.named_ticket_roles(self.pol()[x])})
        self._fin(expect)
        return e

    def override(self, gates: dict[str, dict[str, Any]], *, expect: str = "ok", note: str | None = None,
                 who: str = "sev", **kw: Any) -> dict[str, Any]:  # fmt: skip
        before = self.snap()
        e = self._t("policy.changed", self.w.actor(who), {"gates": copy.deepcopy(gates)}, expect, note=note, **kw)
        if expect == "ok":
            self.t["overrides"].update(copy.deepcopy(gates))
            self.settle(before, set(gates))
        self._fin(expect)
        return e

    def ws_policy(self, gates: dict[str, dict[str, Any]], *, expect: str = "ok", note: str | None = None,
                  who: str = "sev", **kw: Any) -> dict[str, Any]:  # fmt: skip
        before = self.snap()
        self.last_raised = []
        e = self.w.ev("workspace", "policy.changed", self.w.actor(who), {"gates": copy.deepcopy(gates)}, expect=expect,
                      note=note, **kw)  # fmt: skip
        if expect == "ok":
            self.t["ws_policies"].update(copy.deepcopy(gates))
            self.settle(before, set(gates))
        self._fin(expect)
        return e

    def claim(self, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        e = self._t("claim.taken", self.agent, {}, expect, note=note)
        if expect == "ok":
            self.status = "in_progress"
        self._fin(expect)
        return e

    def release(self, *, note: str | None = None) -> dict[str, Any]:
        e = self._t("claim.released", self.agent, {"session": SESSION, "reason": "released"}, note=note)
        if self.status == "in_progress":
            self.status = "open"
        self._fin("ok")
        return e

    def artifact(self, name: str, data: bytes = b"x", *, kind: str = "log", ac: str | None = None,
                 task: str | None = None, actor: dict[str, Any] | None = None, expect: str = "ok", note: str | None = None,
                 replaces: bytes | None = None) -> dict[str, Any]:  # fmt: skip
        pay: dict[str, Any] = {"name": name, "kind": kind, "sha256": g.digest(data), "bytes": len(data)}
        if ac:
            pay["ac"] = ac
        if task:
            pay["task"] = task
        if replaces is not None:
            pay["replaces"] = g.digest(replaces)
        before = self.snap()
        e = self._t(
            "artifact.replaced" if replaces is not None else "artifact.added",
            actor or self.agent,
            pay,
            expect,
            note=note,
        )
        if expect == "ok":
            self.t["artifacts"][name] = {"kind": kind, "bytes_hex": data.hex(), "ac": ac, "task": task}
            marks = {"verify"}
            for x in ("requirements", "plan"):
                sids = g.gate_sections(x, self.t["type"]) + g._addon_sections(self.t, x)
                if any(name in g.refs_of(self.t["sections"].get(sid, "")) for sid in sids):
                    marks.add(x)
            self.settle(before, marks)
        self._fin(expect)
        return e

    def task(self, verb: str, tid: str = "T1", *, receipt: bool = False, expect: str = "ok", note: str | None = None, **extra: Any) -> dict[str, Any]:  # fmt: skip
        pay: dict[str, Any] = {"task": tid, **extra}
        if verb == "skipped":
            pay["reason"] = "not needed"
        if verb == "done" and receipt:
            pay["receipt"] = {"cmd": "echo ok", "exit": 0, "ms": 5, "repo": None, "commit": None}
        before = self.snap()
        e = self._t("task." + verb, self.agent, pay, expect, note=note)
        if expect == "ok":
            if verb == "done" and receipt:
                self.t["receipts"][tid] = {"event": e["id"], "repo": None, "commit": None, "exit": 0}
            elif verb in ("reopened", "skipped"):
                self.t["receipts"].pop(tid, None)
            if verb in ("done", "reopened", "skipped"):
                self.settle(before, {"verify"})
            else:
                self.settle(before, set())
        self._fin(expect)
        return e

    def link_repo(self, branch: str = "feat/x", **kw: Any) -> dict[str, Any]:
        return self.edit(
            {"ticket.links": {"repos": [REPO], "branches": {REPO: branch}, "prs": [], "external": []}}, **kw
        )

    def pushed(self, sha: str, *, branch: str | None = None, remote: str | None = None, expect: str = "ok",
               note: str | None = None, before_override: Any = "auto", repo: str = REPO) -> dict[str, Any]:  # fmt: skip
        cur = self.t["heads"].get(repo)
        cur_entry = None if cur is None else dict(cur)
        remote = remote or self.t["remotes"].get(repo, "")
        branch = branch or self.t["links"]["branches"].get(repo, "feat/x")
        pay = {"repo_name": repo, "repo_id": g.repo_identity(remote, repo), "ref": "refs/heads/" + branch, "sha": sha,
               "before": cur_entry if before_override == "auto" else before_override}  # fmt: skip
        before = self.snap()
        e = self._t("branch.pushed", self.w.HOST, pay, expect, note=note)
        if expect == "ok":
            self.t["remotes"][repo] = remote
            g.observe(self.t, repo, sha, branch)
            self.settle(before, {"verify", "code"})
            if self.status == "done":
                self.status = "testing"
        self._fin(expect)
        return e

    def submit(self, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        e = self._t("ticket.submitted", self.agent, {}, expect, note=note)
        if expect == "ok":
            self.status = "testing"
        self._fin(expect)
        return e

    def close(self, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        e = self._t("ticket.closed", self._sev(), {"resolution": "other"}, expect, note=note)
        if expect == "ok":
            self.status = "closed"
        self._fin(expect)
        return e

    def reopen(self, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        before = self.snap()
        e = self._t("ticket.reopened", self._sev(), {}, expect, note=note)
        if expect == "ok":
            self.status = "open"
            self.settle(before, set(GATES))
        self._fin(expect)
        return e

    def status_changed(self, frm: str, to: str, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        e = self._t("status.changed", self.agent, {"from": frm, "to": to}, expect, note=note)
        if expect == "ok":
            self.status = to
        self._fin(expect)
        return e

    def ticket_restore(self, *, expect: str = "ok", note: str | None = None) -> dict[str, Any]:
        events = self.w.logs[TICKET]
        last = events[-1]
        before = self.snap()
        pay = {
            "from_seq": last["seq"],
            "head": ow.head(last),
            "abandoned": None,
            "abandoned_decisions": [],
            "reason": "restored",
        }
        e = self._t("restore", self._sev(), pay, expect, note=note)
        if expect == "ok":
            self.settle(before, set(GATES))
        self._fin(expect)
        return e

    def ws_restore(self, *, note: str | None = None) -> dict[str, Any]:
        events = self.w.logs["workspace"]
        last = events[-1]
        before = self.snap()
        pay = {
            "from_seq": last["seq"],
            "head": ow.head(last),
            "abandoned": None,
            "abandoned_decisions": [],
            "reason": "restored",
        }
        e = self.w.ev("workspace", "restore", self._sev(), pay, note=note)
        self.settle(before, set(GATES))
        self._fin("ok")
        return e

    def member_removed(self, who: str, *, note: str | None = None, expect: str = "ok") -> dict[str, Any]:
        before = self.snap()
        e = self.w.ev("workspace", "member.removed", self._sev(), {"person": pid(who)}, expect=expect, note=note)
        if expect == "ok":
            marks = self.void(lambda d: d["person"] == pid(who))
            self.settle(before, marks)
        self._fin(expect)
        return e

    def role_changed(self, who: str, role: str, *, note: str | None = None) -> dict[str, Any]:
        before = self.snap()
        e = self.w.ev("workspace", "role.changed", self._sev(), {"person": pid(who), "role": role}, note=note)
        marks = self.void(lambda d: d["person"] == pid(who))
        self.settle(before, marks)
        self._fin("ok")
        return e

    def device_revoked(self, person: str, dev: str, reason: str, *, note: str | None = None) -> dict[str, Any]:
        before = self.snap()
        pay = {"device": did(dev), "reason": reason, "revocation": ow.make_revocation(person, dev, reason)}
        e = self.w.ev("workspace", "device.revoked", self._sev(), pay, note=note)
        marks: set[str] = set()
        if reason == "compromised":
            marks = self.void(lambda d: d["device"] == did(dev))
        self.settle(before, marks)
        self._fin("ok")
        return e

    def void(self, match: Any) -> set[str]:
        """§5.7: approvals of a removed person or compromised device are voided only on gates that have not reached
        ``count``, and not on a done or closed ticket."""
        marks: set[str] = set()
        if self.status in ("done", "closed"):
            return marks
        for x in GATES:
            mine = [d for d in self.counting(x) if match(d)]
            if mine and not self.reached(x):
                for d in mine:
                    d["voided"] = True
                marks.add(x)
        return marks

    def invalidated(
        self, gate: str, cause: str, voided: list[str], *, expect: str = "ok", note: str | None = None
    ) -> dict[str, Any]:
        e = self._t(
            "gate.invalidated", self.w.HOST, {"gate": gate, "cause": cause, "voided": sorted(voided)}, expect, note=note
        )
        self._fin(expect)
        return e

    def external(self, sections: dict[str, str | None], voided: list[str], *, normalised: bool = False, note: str | None = None, expect: str = "ok") -> dict[str, Any]:  # fmt: skip
        pay = {"sections": {s: None if x is None else {"hash": g.section_h(x), "refs": g.refs_of(x)} for s, x in sections.items()},
               "voided_gates": voided, "normalised": normalised}  # fmt: skip
        before = self.snap()
        e = self._t("edit.external", self.w.HOST, pay, expect, note=note)
        if expect == "ok":
            for s, x in sections.items():
                if x is None:
                    self.t["sections"].pop(s, None)
                else:
                    self.t["sections"][s] = x
            self.settle(before, {x for x in GATES if self.bound(x) & {"body." + s for s in sections}})
        self._fin(expect)
        return e

    def addon(self, verb: str, *, note: str | None = None) -> dict[str, Any]:
        before = self.snap()
        binds = {
            "fields": {"points": ["plan"]},
            "sections": [{"id": "estimate.notes", "gate": ["plan"], "types": ["feature", "bug"]}],
        }
        old = self.t["addons"].get("estimate")
        if verb == "granted":
            pay: dict[str, Any] = {"name": "estimate", "version": "1.0.0", "package_sha256": g.digest(b"estimate package"),
                                   "capabilities": ["serve_http"], "binds": binds}  # fmt: skip
        else:
            pay = {"name": "estimate"}
        e = self.w.ev("workspace", "addon." + verb, self._sev(), pay, note=note)
        named = {x for gs in ((old or {}).get("binds", {"fields": {}, "sections": []})["fields"].values()) for x in gs}
        named |= {x for s in (old or {}).get("binds", {"sections": []})["sections"] for x in s["gate"]}
        if verb == "granted":
            named |= {"plan"}
            self.t["addons"]["estimate"] = {"package_sha256": g.digest(b"estimate package"), "binds": binds}
        else:
            self.t["addons"].pop("estimate", None)  # disabled and purged addons are out of G
        self.settle(before, named)
        self._fin("ok")
        return e

    def pin_g(self) -> None:
        """Pin the whole gate hash input of every gate at this step (the readable end-to-end known answer)."""
        self.w.steps[-1]["after"]["tickets"][TICKET]["G"] = {x: self.G(x) for x in GATES}

    def row(self, raised: list[str]) -> None:
        """The author's literal reading of the §5.7 table for the step just made; the tracker must agree (this stops
        the oracle otherwise) and the step records it, so ``orch.model`` is held to it too."""
        assert self.last_raised == raised, (self.w.steps[-1].get("note"), self.last_raised, raised)
        self.w.steps[-1]["raised"] = raised

    def scenario(self, **extra: Any) -> dict[str, Any]:
        return self.w.scenario(**extra)


def fill_and_approve(s: Sim) -> None:
    s.ready()
    s.decide("approve", "requirements", note="sev approves requirements")
    s.decide("approve", "plan", note="sev approves plan")


# --- the scenarios ----------------------------------------------------------------------------------------------------


def _new(name: str, pins: str, **kw: Any) -> Sim:
    return Sim(name, pins, **kw)


def generation_scenarios() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    R, P, V, C = "requirements", "plan", "verify", "code"

    # ticket.updated rows
    s = _new("raise_ticket_updated", "ticket.updated with a path in bound(g) raises g in every status, whether or not the value changed, "
             "and a raise of an earlier gate raises the later ones; unbound paths raise nothing")  # fmt: skip
    s.ready()
    s.row([R, P, V])
    s.edit({"ticket.size": "m"}, note="the same value again")
    s.row([R, P, V])
    s.edit(sections={"verification": "ran it twice"})
    s.row([V])
    s.edit(sections={"plan": "2. do it another way"})
    s.row([P, V])
    s.edit({"ticket.tasks": [{"id": "T1", "text": "do it", "verify": {"cmd": "echo ok"}, "proves": ["AC1"]}]})
    s.row([P, V])
    s.edit(sections={"requirements": "req 2"})
    s.row([R, P, V])
    s.edit({"ticket.title": "Another title"}, note="the title is not bound by any gate")
    s.row([])
    s.edit(sections={"current_state": "handoff"}, note="current state is not bound by any gate")
    s.row([])
    s.link_repo()
    s.row([V])
    s.edit({"ticket.size": "l"}, sections={"requirements": "req 3", "verification": "ran it 3"}, note="several rows match: one raise each")  # fmt: skip
    s.row([R, P, V])
    out.append(s.scenario())

    # the code gate when it applies
    s = _new(
        "raise_with_the_code_gate",
        "once the code gate applies it is raised by the rows that name it: policy, links, every earlier gate",
    )
    s.ready()
    s.ws_policy({C: CODE_ON}, note="the workspace turns the code gate on")
    s.row([C])
    s.link_repo()
    s.row([V, C])
    s.edit(sections={"verification": "ran it twice"})
    s.row([V, C])
    s.edit(sections={"plan": "2. other"})
    s.row([P, V, C])
    s.edit({"ticket.size": "l"})
    s.row([R, P, V, C])
    s.ws_policy(
        {C: {**CODE_ON, "applies": ["bug"]}}, note="applies a list that leaves a feature out: code stops applying"
    )
    s.row([C])
    s.edit(sections={"verification": "again"}, note="code does not apply, so it is not raised")
    s.row([V])
    out.append(s.scenario())

    # edit.external
    s = _new(
        "raise_edit_external",
        "edit.external naming a section of g raises g; voided_gates is derived (the gates that lost a counting approval)",
    )
    s.ready()
    s.decide("approve", R)
    s.row([P, V])
    s.external(
        {"context": "ctx changed outside"}, [R], note="host: body.md changed; the approval of requirements is voided"
    )
    s.row([R, P, V])
    s.external({"verification": "changed outside"}, [], note="no approval on verify to void")
    s.row([V])
    s.external({"current_state": "x"}, [], note="not bound")
    s.row([])
    s.external(
        {"plan": "p"},
        [R],
        expect="auth.invalid_event",
        note="voided_gates is derived: a wrong list is an authorization failure",
    )
    out.append(s.scenario())

    # artifacts
    s = _new(
        "raise_artifacts",
        "artifact.added/replaced raise verify; requirements/plan only when the name is in the refs of one of their sections",
    )
    s.ready()
    s.artifact("note.log", b"v1")
    s.row([V])
    s.edit(sections={"plan": "see (artifact:note.log)"})
    s.row([P, V])
    s.artifact("note.log", b"v2", replaces=b"v1", note="replaced: named in the refs of a plan section")
    s.row([P, V])
    s.artifact("other.log", b"o1", note="not named by any section")
    s.row([V])
    s.edit(sections={"context": "see (artifact:other.log)"})
    s.row([R, P, V])
    s.artifact("other.log", b"o2", replaces=b"o1", note="now named by a requirements section")
    s.row([R, P, V])
    s.edit(
        sections={"decisions": "uses (artifact:ghost.png)"},
        expect="body.unknown_artifact",
        note="an unknown reference is refused",
    )
    s.artifact(
        "note.log", b"v3", replaces=b"nope", expect="artifact.bad_replaces", note="replaces must be the current digest"
    )
    out.append(s.scenario())

    # tasks
    s = _new(
        "raise_tasks", "task.done, task.reopened and task.skipped raise verify; a receipt enters the verify gate hash"
    )
    s.ready()
    s.claim()
    s.row([])
    s.task("started")
    s.row([])
    s.task("done")
    s.row([V])
    s.task("reopened")
    s.row([V])
    s.task("done", receipt=True, note="done with a receipt: it is in G.receipts")
    s.row([V])
    s.task("reopened")
    s.row([V])
    s.task("skipped")
    s.row([V])
    out.append(s.scenario())

    # branch.pushed and the source list
    s = _new("raise_branch_pushed_and_source_list", "branch.pushed raises verify and code; the source list is the projection of the latest "
             "branch.pushed per linked repo (first observation, new commit, force-push back, ref change, identity change)")  # fmt: skip
    s.ready()
    s.ws_policy({C: CODE_ON})
    s.link_repo("feat/x")
    s.pushed(SHA_A, note="first observation: before is null")
    s.row([V, C])
    s.pushed(SHA_B, note="a new commit")
    s.row([V, C])
    s.pushed(SHA_B, expect="source.not_new", note="nothing changed")
    s.pushed(SHA_C, before_override=None, expect="source.not_new", note="`before` is not the current entry")
    s.pushed(SHA_A, note="a reset back to an older commit is a new observation")
    s.row([V, C])
    s.pushed(
        SHA_C,
        repo="other-repo",
        remote="https://github.com/acme/other",
        expect="source.unlinked",
        note="not in links.repos",
    )
    s.link_repo("feat/y", note="the ticket moves to another branch")
    s.row([V, C])
    s.pushed(SHA_C, branch="feat/y", note="the ref changed")
    s.row([V, C])
    s.pushed(
        SHA_C,
        remote="https://git.example.com:8443/Acme/energy-dbt.git",
        note="the remote changed: another repo identity",
    )
    s.row([V, C])
    out.append(s.scenario())

    # people
    s = _new(
        "raise_people",
        "people.changed raises g only for a role named in g's effective policy (or assignees when independent)",
    )
    s.ready()
    s.people("watchers", ["lena"])
    s.row([])
    s.people("reviewers", ["mara"], note="verify's approvers name reviewers")
    s.row([V])
    s.people("assignees", ["lena"], note="verify's `not` names assignees")
    s.row([V])
    s.people("owner", ["mara"], note="no policy names ticket_owner yet")
    s.row([])
    s.ws_policy({R: policy(["owner", "ticket_owner"])}, note="requirements now names ticket_owner")
    s.row([R, P, V])
    s.people("owner", ["sev"], note="the owner changes back")
    s.row([R, P, V])
    s.ws_policy({C: CODE_ON})
    s.row([C])
    s.people("assignees", ["mara"], note="code's `not` names assignees too")
    s.row([V, C])
    out.append(s.scenario())

    # policies
    s = _new(
        "raise_policy",
        "policy.changed (ticket override or workspace) whose gates name g raises g; an override can switch a gate on (applies is a union)",
    )
    s.ready()
    s.override({P: policy(["owner"], 2)}, note="ticket override on plan")
    s.row([P, V])
    s.ws_policy({V: policy(["reviewers"], 1, ["assignees"], "all", True)}, note="workspace names verify")
    s.row([V])
    s.ws_policy({R: policy(["owner", "maintainer"])}, note="workspace names requirements")
    s.row([R, P, V])
    s.override({C: CODE_ON}, note="override applies 'all' + workspace 'off' = all: the code gate now applies")
    s.row([C])
    out.append(s.scenario())

    # addons
    s = _new("raise_addons", "addon.granted/disabled/purged whose old or new binds name g raise g")
    s.ready()
    s.edit({"ticket.addons.estimate.points": 5}, expect="addon.unknown", note="no such addon yet")
    s.addon("granted")
    s.row([P, V])
    s.edit(
        {"ticket.addons.estimate.points": 5},
        sections={"estimate.notes": "five points"},
        note="an addon field and section bound to plan",
    )
    s.row([P, V])
    s.addon("disabled")
    s.row([P, V])
    s.addon("granted", note="granted again: enabled")
    s.row([P, V])
    s.addon("purged")
    s.row([P, V])
    out.append(s.scenario())

    # change requests and the first-count cascade
    s = _new("raise_change_requests_and_approvals", "gate.changes_requested on g raises g and every later gate; a change of an earlier gate's first-count "
             "approvals raises the later gates")  # fmt: skip
    s.ready()
    s.decide("approve", R)
    s.row([P, V])
    s.decide("approve", P)
    s.row([V])
    s.decide("changes", P, note="plan: changes requested")
    s.row([P, V])
    s.decide("changes", R, note="requirements: changes requested")
    s.row([R, P, V])
    out.append(s.scenario())

    # reopen and restore
    s = _new(
        "raise_reopen_and_restore",
        "ticket.reopened and restore raise every gate; bound edits are refused on closed tickets",
    )
    s.ready()
    s.close()
    s.edit({"ticket.size": "l"}, expect="ticket.frozen", note="bound paths are refused on a closed ticket")
    s.reopen()
    s.row([R, P, V])
    s.ticket_restore(note="restore in the ticket log")
    s.row([R, P, V])
    s.ws_restore(note="restore in the workspace log reaches every ticket")
    s.row([R, P, V])
    out.append(s.scenario())

    # delayed approval
    s = _new("delayed_approval_is_stale", "a decision carries the gate_gen its signer saw: after a change request a delayed approval is refused "
             "(gate.stale), and so is one with a stale hash or policy hash")  # fmt: skip
    s.ready()
    old_gen = s.gen[P]
    s.decide("changes", P, note="a change request lands first")
    s.row([P, V])
    s.decide("approve", P, gen=old_gen, expect="gate.stale", note="signed before the change request, appended after it")
    s.decide(
        "approve",
        P,
        stale={"hash": g.digest(b"not the gate hash")},
        expect="gate.stale",
        note="right generation, but not the current gate hash",
    )
    s.decide(
        "approve",
        P,
        stale={"policy_hash": g.digest(b"other policy")},
        expect="gate.stale",
        note="not the current policy hash",
    )
    s.decide("approve", P, note="the approval with the current generation and hash")
    out.append(s.scenario())

    # reverted edit
    s = _new("reverted_edit_does_not_revive_an_approval", "reverting the content brings back the old hash but not the old generation: the retired "
             "approval stays retired")  # fmt: skip
    s.ready()
    s.decide("approve", R)
    old_gen, old_hash = s.gen[R], s.hash(R)
    s.edit(sections={"context": "ctx B"})
    s.row([R, P, V])
    s.edit(sections={"context": "ctx"}, note="reverted: same text, same section hash")
    s.row([R, P, V])
    s.decide(
        "approve",
        R,
        gen=old_gen,
        stale={"hash": old_hash},
        expect="gate.stale",
        note="the old approval's generation and hash",
    )
    s.decide("approve", R, gen=old_gen, expect="gate.stale", note="old generation with the current hash")
    s.decide("approve", R, note="a fresh approval at the current generation counts")
    out.append(s.scenario())

    # voided approvals
    def two(name: str, pins: str) -> Sim:
        x = _new(name, pins)
        x.ws_policy({R: policy(["maintainer", "owner"], 2)}, note="requirements needs two approvers")
        x.ready()
        x.decide("approve", R, "mara", note="one of two")
        x.row([P, V])
        return x

    s = two(
        "voided_by_member_removed",
        "member.removed voids that person's approvals on gates that have not reached count, and raises the gate",
    )
    s.member_removed("mara")
    s.row([R, P, V])
    ids = [d["id"] for d in s.dec[R]]
    s.invalidated(R, "member_changed", ids, note="gate.invalidated records the voided approvals and raises nothing")
    s.row([])
    s.invalidated(
        R,
        "member_changed",
        ids,
        expect="gate.invalidated_mismatch",
        note="voided is derived: listing it twice is refused",
    )
    out.append(s.scenario())

    s = two("voided_by_role_changed", "role.changed that takes the approver token away voids the approval the same way")
    s.role_changed("mara", "member")
    s.row([R, P, V])
    out.append(s.scenario())

    s = two(
        "voided_by_compromised_device",
        "device.revoked (compromised) voids what that device signed; other reasons do not",
    )
    s.device_revoked("lena", "lena1", "lost", note="an unrelated device, reason lost: nothing")
    s.row([])
    s.device_revoked("mara", "mara1", "compromised")
    s.row([R, P, V])
    out.append(s.scenario())

    s = _new(
        "approval_that_reached_count_is_not_voided",
        "once a gate reached count, a later removal does not void its approvals",
    )
    s.ws_policy({R: policy(["maintainer", "owner"], 1)})
    s.ready()
    s.decide("approve", R, "mara")
    s.row([P, V])
    s.member_removed("mara")
    s.row([])
    out.append(s.scenario())
    return out


def _to_done(s: Sim) -> None:
    """Decisions in each state on the way to ``done`` (5.9): approvals outside done/closed leave the status, verdicts and
    code decisions only in testing, a non-completing pass leaves it, the completing decision makes it done."""
    R, P, V, C = "requirements", "plan", "verify", "code"
    s.ws_policy({C: CODE_ON}, note="the code gate applies")
    s.ready()
    s.people("reviewers", ["sev"], note="sev reviews (verify approvers: reviewers)")
    s.decide("approve", R, note="requirements approval in open: status unchanged")
    s.decide("approve", P, note="plan approval in open")
    s.decide("pass", V, expect="gate.status", note="verdicts only in testing")
    s.decide("approve", C, "mara", expect="gate.status", note="code decisions only in testing")
    s.status_changed("open", "backlog")
    s.decide("approve", P, note="a duplicate approval by the same person is accepted and counts once")
    s.status_changed("backlog", "open")
    s.claim()
    s.decide("pass", V, expect="gate.status", note="verdicts only in testing, not in progress")
    s.link_repo("feat/x")
    s.pushed(SHA_A)
    s.artifact("proof.log", b"proof", ac="AC1")
    s.task("done")
    s.submit(note="requirements and plan approved, evidence for every AC, verification written")
    s.decide("changes", P, note="changes requested on plan while testing: back to in_progress")
    s.submit(expect="submit.incomplete", note="plan is no longer approved")
    s.decide("approve", P, note="plan approved again")
    s.submit()
    s.decide("fail", V, note="a failing verdict: testing -> in_progress")
    s.submit(note="a failing verdict raises verify and later gates only: requirements and plan still count")
    s.decide("pass", V, note="a passing verdict that does not complete the done rule (code is missing): stays testing")
    s.decide("approve", C, "mara", note="the code approval completes the done rule: done")


def status_scenarios() -> list[dict[str, Any]]:
    V = "verify"
    pins = "ticket-format 5.9 along one ticket: "
    out = []
    s = _new("status_table", pins + "decisions in each state, verdicts and code decisions only in testing, a pass that does not complete the done "
             "rule leaves the status, done is sticky, close and reopen")  # fmt: skip
    _to_done(s)
    s.pin_g()
    s.decide("pass", V, "sev", expect="gate.status", note="no decisions on a done ticket")
    s.edit({"ticket.size": "l"}, expect="ticket.frozen", note="done: bound paths are refused from every actor")
    s.people("watchers", ["lena"], note="a watcher on a done ticket changes nothing")
    s.ws_policy(
        {V: policy(["reviewers"], 1, ["assignees"], "all", True)},
        note="a workspace policy change raises generations but done is sticky",
    )
    s.close(note="close from done")
    s.decide("pass", V, expect="gate.status", note="no decisions on a closed ticket")
    s.close(expect="status.transition", note="already closed")
    s.reopen(note="reopen from closed")
    s.reopen(expect="status.transition", note="reopen from open")
    out.append(s.scenario())

    s = _new("done_goes_back_to_testing_on_a_push", pins + "branch.pushed for a source ref sends a done ticket back to testing; a new "
             "sha counts, an identity or ref change on a done ticket is refused")  # fmt: skip
    _to_done(s)
    s.pushed(
        SHA_C,
        remote="https://git.example.com/acme/energy-dbt.git",
        expect="source.not_new",
        note="on a done ticket only a new sha on an existing ref counts",
    )
    s.pushed(SHA_B, note="a new sha on the existing ref: back to testing")
    s.decide("pass", V, note="the old decisions no longer count; a new verdict is needed")
    s.decide("approve", "code", "mara", note="done again")
    out.append(s.scenario())

    s = _new("done_then_reopened", pins + "after a reopen the old verdict and code approval no longer count")
    _to_done(s)
    s.reopen(note="reopen from done: every gate is raised")
    out.append(s.scenario())
    return out


def effective_policy_cases() -> list[dict[str, Any]]:
    """Pure cases of the §5.7 intersection, independent of any ticket."""
    p = policy
    cases = [
        (
            "override_none_is_the_workspace_policy_in_canonical_form",
            {
                "approvers": ["owner", "maintainer", "owner"],
                "count": 1,
                "not": [],
                "applies": "all",
                "independent": False,
            },
            None,
        ),
        ("approvers_intersect", p(["owner", "maintainer"]), p(["owner", "reviewers"])),
        ("count_is_the_larger", p(["owner"], 2), p(["owner"], 1)),
        ("count_larger_from_the_override", p(["owner"], 1), p(["owner"], 3)),
        (
            "not_is_the_union",
            p(["owner", "maintainer"], 1, ["assignees"]),
            p(["owner", "maintainer"], 1, ["reviewers", "assignees"]),
        ),
        ("independent_is_either", p(["owner"], 1, (), "all", False), p(["owner"], 1, (), "all", True)),
        ("applies_all_wins", p(["owner"], 1, (), "all"), p(["owner"], 1, (), ["bug"])),
        ("applies_off_and_a_list_is_the_list", p(["owner"], 1, (), "off"), p(["owner"], 1, (), ["spike", "bug"])),
        ("applies_off_only_if_both_off", p(["owner"], 1, (), "off"), p(["owner"], 1, (), "off")),
        ("applies_lists_union_sorted", p(["owner"], 1, (), ["feature", "bug"]), p(["owner"], 1, (), ["bug", "chore"])),
        (
            "a_list_naming_every_type_stays_a_list",
            p(["owner"], 1, (), ["feature", "bug", "chore"]),
            p(["owner"], 1, (), ["spike", "epic"]),
        ),
        ("no_common_token_leaves_the_gate_blocked", p(["owner"]), p(["reviewers"])),
        ("override_cannot_loosen_not", p(["owner"], 1, ["assignees"]), p(["owner"], 1, [])),
        (
            "code_policy",
            p(["maintainer", "owner"], 1, ["assignees"], "off", True),
            p(["owner"], 2, ["assignees"], "all", True),
        ),
    ]
    return [{"name": n, "workspace": w, "override": o, "effective": g.effective_policy(w, o)} for n, w, o in cases]


def effective_policy_scenarios() -> list[dict[str, Any]]:
    R, P = "requirements", "plan"
    s = _new("effective_policy_follows_both_sides", "the effective policy is recomputed whenever either side changes: an override never ends up "
             "looser, an override that leaves no token is refused, a workspace change that empties the set blocks the gate")  # fmt: skip
    s.full_policy = True
    s.ws_policy({R: policy(["owner", "maintainer"], 1)}, note="workspace: owner or maintainer, one approval")
    s.ready()
    s.override({R: policy(["owner"], 2)}, note="the override tightens: owner only, two approvals")
    s.override(
        {R: policy(["reviewers"], 1)},
        expect="gate.no_eligible",
        note="an override that leaves no approver token is refused",
    )
    s.override({R: policy(["owner"], 1)}, note="an override with a lower count: the effective count stays 2")
    s.ws_policy(
        {R: policy(["maintainer"], 3)}, note="a later workspace change empties the intersection: blocked, count 3"
    )
    s.decide("approve", R, expect="gate.no_eligible", note="a blocked gate takes no decisions")
    s.ws_policy(
        {R: policy(["owner", "maintainer"], 1)},
        note="the workspace fixes its policy: the override's tightening is still there",
    )
    s.ws_policy({P: policy(["owner"], 1, (), ["bug"])}, note="plan stops applying to features")
    s.override({P: policy(["owner"], 1, (), ["feature"])}, note="applies is a union: plan applies again")
    s.override(
        {"code": policy(["owner"], 1, [], "all", True)},
        expect="policy.invalid",
        note="code's `not` must include assignees (D59)",
        schema_refused=True,
    )
    s.ws_policy(
        {"code": policy(["owner"], 1, ["assignees"], "off", False)},
        expect="policy.invalid",
        note="code is always independent (D59)",
        schema_refused=True,
    )
    return [s.scenario()]


def question_ids(ticket: str, qn: str, text: str, options: list[dict[str, str]]) -> tuple[str, str]:
    """``qid`` and the question hash (§5.6), independently."""
    qid = hashlib.sha256(
        b"orch/v2/question-id|" + ow.cj({"workspace_id": ow.W, "ticket": ticket, "question": qn})
    ).hexdigest()[:32]
    body = {"question_id": qid, "ticket": ticket, "text": text, "options": options}
    return qid, "sha256:" + hashlib.sha256(b"orch/v2/question|" + ow.cj(body)).hexdigest()


def question_scenarios() -> list[dict[str, Any]]:
    text = "Which tariff export is the source of truth?"
    opts = [{"key": "csv", "label": "Monthly CSV"}, {"key": "api", "label": "Tariff API"}]
    qid, qh = question_ids(TICKET, "Q1", text, opts)
    question = {"id": "Q1", "to": "ticket_owner", "text": text, "options": opts, "blocking": True}

    def base(name: str, pins: str) -> tuple[Sim, dict[str, Any]]:
        s = Sim(name, pins)
        asked = s._t(
            "question.asked", s.agent, {"question": question, "qid": qid, "hash": qh}, note="the agent asks Q1"
        )
        return s, asked

    out = []
    s, asked = base(
        "question_ids_and_answers",
        "the qid and the question hash are derived (5.6); the first valid signed answer for the current hash wins",
    )
    s._t(
        "question.asked",
        s.agent,
        {"question": {**question, "id": "Q2"}, "qid": qid, "hash": qh},
        expect="question.bad_id",
        note="qid is derived from the question id",
    )
    s._t(
        "question.asked",
        s.agent,
        {"question": question, "qid": qid, "hash": g.digest(b"x")},
        expect="question.bad_hash",
        note="hash is the question hash",
    )
    s._t(
        "question.answered",
        s.w.actor("lena"),
        {"question": "Q1", "hash": qh, "option": "csv"},
        expect="answer.not_allowed",
        note="a member who is not an addressee",
    )
    s._t(
        "question.answered",
        s._sev(),
        {"question": "Q1", "hash": g.digest(b"old"), "option": "csv"},
        expect="question.stale",
        note="the question changed since it was shown",
    )
    s._t(
        "question.answered",
        s._sev(),
        {"question": "Q1", "hash": qh, "option": "pdf"},
        expect="answer.bad_option",
        note="not an option",
    )
    answer = s._t(
        "question.answered", s._sev(), {"question": "Q1", "hash": qh, "option": "csv"}, note="the owner answers"
    )
    s._t(
        "question.answered",
        s.w.actor("mara"),
        {"question": "Q1", "hash": qh, "option": "api"},
        expect="question.answered",
        note="the first valid answer won",
    )
    out.append({**s.scenario(), "qid": qid, "hash": qh, "answer_id": answer["id"]})

    # the same qid and text after a restore: the abandoned decision must not come back (F1 5.10, review finding 1)
    for name, replay, pins in (
        ("question_fresh_answer_after_restore", False, "after a restore to the question, the same question (same qid and hash) can be answered afresh"),
        ("question_replayed_decision_after_restore", True, "a restore abandons a signed answer (listed in abandoned_decisions): the same signed event "
         "cannot be appended again on the new chain"),
    ):  # fmt: skip
        s, asked = base(name, pins)
        answered = s._t(
            "question.answered",
            s._sev(),
            {"question": "Q1", "hash": qh, "option": "csv"},
            note="(the answer that the rollback gives up)",
        )
        gone = s.w.logs[TICKET].pop()  # the new chain does not have it, and neither does the scenario
        s.w.steps.pop()
        restore = {"from_seq": asked["seq"], "head": ow.head(asked), "abandoned": {"seq": answered["seq"], "head": ow.head(gone)},
                   "abandoned_decisions": [answered["id"]], "reason": "restored a backup"}  # fmt: skip
        s._t("restore", s._sev(), restore, note="the owner restores to the question")
        if replay:
            s.w.raw(
                TICKET,
                answered,
                expect="refused",
                note="the abandoned signed answer appended again: same id, signature and based_on, a new seq and host_sig",
            )
        else:
            s._t(
                "question.answered",
                s._sev(),
                {"question": "Q1", "hash": qh, "option": "csv"},
                note="a fresh answer with a new id and signature",
            )
        out.append({**s.scenario(), "qid": qid, "hash": qh, "abandoned_answer": gone})
    return out
