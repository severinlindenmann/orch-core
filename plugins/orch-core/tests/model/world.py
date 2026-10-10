"""Test helper: build valid, chained event logs (schema-checked) and replay them through ``orch.model``."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from orch import canon, schema
from orch.model import FakeVerifier, admit, replay
from tests.schema.examples import cert, digest, hex32, pub, sig

B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
T0 = datetime(2026, 10, 9, 9, 0, 0, tzinfo=UTC)
REPO = "https://github.com/acme/energy-dbt"
SHA1, SHA2, SHA3 = "a1" * 20, "b2" * 20, "c3" * 20
REFS = re.compile(r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)")


def ulid(n: int) -> str:
    s = ""
    for _ in range(12):
        s = B32[n % 32] + s
        n //= 32
    return "01J9ZK" + "0" * 8 + s


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """A workspace being written. ``w.state()`` replays everything; ``w.try_()`` appends only if ``admit`` allows."""

    HOST = {"kind": "host"}

    def __init__(self, verifier: FakeVerifier | None = None, validate: bool = True):
        self.verifier = verifier or FakeVerifier()
        self.validate = validate
        self.ws: list[dict[str, Any]] = []
        self.tl: dict[str, list[dict[str, Any]]] = {}
        self.n = 0
        self.clock = T0
        self.roster_v = 0
        self.people: dict[str, str] = {}
        self.roles: dict[str, str] = {"sev": "owner"}
        self.dev: dict[str, str] = {}
        self.uid_n = 0
        self.workspace_id = hex32("workspace")

    # ---- plumbing
    def tick(self, seconds: int = 10) -> str:
        self.clock += timedelta(seconds=seconds)
        return stamp(self.clock)

    def at(self) -> str:
        return stamp(self.clock)

    def nid(self) -> str:
        self.n += 1
        return ulid(self.n)

    def actor(self, name: str) -> dict[str, Any]:
        return {"kind": "person", "id": self.people[name], "device": self.dev[name]}

    def _build(self, log: str, typ: str, actor: dict[str, Any], payload: dict[str, Any], **env: Any) -> dict[str, Any]:
        events = self.ws if log == "workspace" else self.tl.get(log, [])
        prev = canon.event_head(events[-1]) if events else None
        e: dict[str, Any] = {
            "v": 2,
            "id": env.pop("id", None) or self.nid(),
            "seq": len(events) + 1,
            "at": env.pop("at", None) or self.tick(),
            "type": typ,
            "actor": actor,
            "based_on": prev,
            "prev": prev,
            "hash_v": 1,
            "host_sig": sig("host" + str(self.n)),
        }
        if log != "workspace":
            e["ws_seq"] = len(self.ws)
        if actor["kind"] == "person":
            e.update(
                auth="passphrase",
                sig=sig("p" + str(self.n)),
                roster_v=0 if typ == "workspace.created" else self.roster_v,
            )
        e.update(payload)
        e.update(env)
        if "based_on_override" in e:
            e["based_on"] = e.pop("based_on_override")
        return e

    def _append(self, log: str, e: dict[str, Any]) -> dict[str, Any]:
        if self.validate:
            schema.validate("event." + e["type"], e, log="workspace" if log == "workspace" else "ticket")
        (self.ws if log == "workspace" else self.tl.setdefault(log, [])).append(e)
        if log == "workspace" and e["type"] in ("workspace.created", "member.added", "member.removed", "role.changed"):
            self.roster_v += 1
        return e

    @staticmethod
    def _env(payload: dict[str, Any]) -> dict[str, Any]:
        return {k: payload.pop(k) for k in ("roster_v", "at", "id", "based_on_override", "ws_seq") if k in payload}

    def wev(self, typ: str, actor: str | dict, **payload: Any) -> dict[str, Any]:
        a = self.actor(actor) if isinstance(actor, str) else actor
        env = self._env(payload)
        return self._append("workspace", self._build("workspace", typ, a, payload, **env))

    def tev(self, uid: str, typ: str, actor: str | dict, **payload: Any) -> dict[str, Any]:
        a = self.actor(actor) if isinstance(actor, str) else actor
        env = self._env(payload)
        return self._append(uid, self._build(uid, typ, a, payload, **env))

    # ---- inspection
    def state(self, now: str | None = None):
        return replay(self.ws, self.tl, verifier=self.verifier, now=now or self.at())

    def build_unappended(self, log: str, typ: str, actor, **payload):
        a = self.actor(actor) if isinstance(actor, str) else actor
        env = self._env(payload)
        e = self._build(log, typ, a, payload, **env)
        e.pop("host_sig", None)
        return e

    def try_(self, log: str, typ: str, actor, **payload):
        """Build an event and append it only if ``admit`` allows it; returns (event, result)."""
        st = self.state()
        e = self.build_unappended(log, typ, actor, **payload)
        res = admit(st, e, log=log)
        if hasattr(res, "code"):
            return e, res
        e["host_sig"] = sig("host" + str(self.n))
        self._append(log, e)
        return e, res

    # ---- workspace setup
    def bootstrap(self, members: dict[str, str] | None = None):
        """sev (owner) creates the workspace; ``members`` maps name -> role, added by sev."""
        self.add_person("sev")
        o = self.people["sev"]
        owner_hex = o[2:]
        dcert = cert(owner_hex, self.dev["sev"][2:])
        self.wev(
            "workspace.created",
            "sev",
            workspace_id=self.workspace_id,
            prefix="DEMO",
            host_id="h_" + ulid(999_999),
            wsk_pub=pub("wsk"),
            owner={"person": o, "name": "sev", "pk_pub": pub("pk-sev")},
            delegation={
                "o": {
                    "v": 2,
                    "suite": 2,
                    "kind": "ws_delegation",
                    "workspace_id": self.workspace_id,
                    "wsk_pub": pub("wsk"),
                    "owner_person_id": owner_hex,
                    "client_hosted": False,
                    "issued_ms": 1_760_000_000_000,
                },
                "sig": sig("delegation"),
            },
            device_cert=dcert,
        )
        for name, role in (members or {}).items():
            self.member(name, role)
        return self

    def add_person(self, name: str) -> None:
        self.people[name] = "p_" + hex32("person-" + name)
        self.dev[name] = "d_" + hex32("device-" + name)

    def member(self, name: str, role: str, by: str = "sev") -> dict[str, Any]:
        self.add_person(name)
        self.roles[name] = role
        return self.wev(
            "member.added",
            by,
            person=self.people[name],
            name=name,
            role=role,
            pk_pub=pub("pk-" + name),
            device_cert=cert(self.people[name][2:], self.dev[name][2:]),
        )

    def grant(self, name: str, scope: str = "all", verbs: Any = "agent", hours: int = 8) -> str:
        gid = "gr_" + ulid(self.n + 100_000)
        now = self.tick()
        issued = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.wev(
            "grant.issued",
            name,
            grant=gid,
            scope=scope,
            verbs=verbs,
            issued_at=now,
            hours=hours,
            expires_at=stamp(issued + timedelta(hours=hours)),
            secret_hash=digest("secret" + gid),
            at=now,
        )
        return gid

    def agent(self, name: str, gid: str, session: str | None = None) -> dict[str, Any]:
        return {
            "kind": "agent",
            "id": "claude-code",
            "session": session or "s_" + ulid(7),
            "for": self.people[name],
            "grant": gid,
        }

    def unattended(self, session: str | None = None) -> dict[str, Any]:
        return {"kind": "agent", "id": "claude-code", "session": session or "s_" + ulid(8), "unattended": True}

    def settings(self, **kw: Any):
        return self.wev("settings.changed", "sev", set=kw)

    # ---- tickets
    def ticket(self, owner: str = "sev", type_: str = "feature", title: str = "A ticket") -> str:
        self.uid_n += 1
        uid = "01J9ZK4Q7M3R8T2V6X0B" + f"{self.uid_n:06d}"
        key = f"DEMO-{self.uid_n:04d}"
        self.tev(uid, "ticket.created", owner, key=key, ticket_type=type_, title=title, owner=self.people[owner])
        return uid

    def view(self, uid: str, now: str | None = None):
        return self.state(now).tickets[uid]

    def edit(
        self, uid: str, actor, sets: dict[str, Any] | None = None, sections: dict[str, str | None] | None = None, **kw
    ):
        """A ``ticket.updated`` whose ``base_rev`` is read from the current state; section values are prose."""
        v = self.view(uid)
        base: dict[str, str] = {}
        payload: dict[str, Any] = {}
        for path, val in (sets or {}).items():
            key = path[len("ticket.") :]
            if key.startswith("addons."):
                cur = (v.fields["addons"].get(key.split(".")[1]) or {}).get(key.split(".")[2])
            else:
                cur = v.fields[key]
            base[path] = canon.value_hash(_thaw(cur))
            payload.setdefault("set", {})[path] = val
        for sid, text in (sections or {}).items():
            base["body." + sid] = v.sections[sid]["hash"] if sid in v.sections else canon.section_hash("")
            payload.setdefault("sections", {})[sid] = (
                None if text is None else {"hash": canon.section_hash(text), "refs": sorted(set(REFS.findall(text)))}
            )
        return self.tev(uid, "ticket.updated", actor, base_rev=base, **payload, **kw)

    def fill(self, uid: str, actor="sev", with_tasks: bool = True):
        """Everything requirements and plan approvals need for a feature: sections, one AC, one task."""
        secs = {
            s: f"{s} text" for s in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification")
        }
        self.edit(uid, actor, {"ticket.acceptance": [{"id": "AC1", "text": "it works"}]}, secs)
        if with_tasks:
            tasks = [{"id": "T1", "text": "do it", "verify": {"cmd": "make test"}, "proves": ["AC1"]}]
            self.edit(uid, actor, {"ticket.tasks": tasks})

    def decide(
        self, uid: str, who: str, gate: str, kind: str = "approve", text: str = "x", try_: bool = False, **over: Any
    ):
        """A gate decision signed against the current gate hash, generation, policy hash and source list."""
        v = self.view(uid)
        g = v.gates[gate]
        p: dict[str, Any] = {"gate_gen": g.gen, "hash": g.hash, "policy_hash": g.policy_hash}
        sl = [dict(x) for x in v.source_list]
        if gate == "verify":
            typ = "verdict.given"
            p.update(outcome="pass" if kind == "approve" else "fail", source_sha=sl)
            if kind != "approve":
                p["text"] = text
        elif kind == "approve":
            typ = "gate.approved"
            p["gate"] = gate
            if gate == "code":
                p["source_sha"] = sl
        else:
            typ = "gate.changes_requested"
            p.update(gate=gate, text=text)
        p.update(over)
        return self.try_(uid, typ, who, **p) if try_ else self.tev(uid, typ, who, **p)

    def push(self, uid: str, sha: str = SHA1, repo: str = "dbt", repo_id: str = REPO, ref: str = "refs/heads/feat/x"):
        cur = None
        for e in reversed(self.tl[uid]):
            if e["type"] == "branch.pushed" and e["repo_name"] == repo:
                cur = {"repo_id": e["repo_id"], "ref": e["ref"], "sha": e["sha"]}
                break
        return self.tev(uid, "branch.pushed", self.HOST, repo_name=repo, repo_id=repo_id, ref=ref, sha=sha, before=cur)

    def with_repo(self, uid: str, actor="sev", name: str = "dbt"):
        """Configure a repo, link it with a branch and let the host observe its first push."""
        self.settings(repos={name: {"path": "../" + name}})
        links = {"repos": [name], "branches": {name: "feat/x"}, "prs": [], "external": []}
        self.edit(uid, actor, {"ticket.links": links})
        return self.push(uid, repo=name)

    def claim(self, uid: str, name: str = "sev", session: str | None = None, gid: str | None = None, **kw):
        gid = gid or self.grant(name, "workable" if self.roles.get(name) == "member" else "all")
        a = self.agent(name, gid, session)
        self.tev(uid, "claim.taken", a, **kw)
        return a

    def to_testing(self, uid: str, name: str = "sev", repo: bool = True):
        """Ticket with approved requirements and plan, claimed, task done with evidence, submitted."""
        self.fill(uid)
        if repo:
            self.with_repo(uid)
        a = self.claim(uid, name)
        self.decide(uid, "sev", "requirements")
        self.decide(uid, "sev", "plan")
        receipt = {
            "cmd": "make test",
            "exit": 0,
            "ms": 5,
            "repo": "dbt" if repo else None,
            "commit": SHA1 if repo else None,
        }
        self.tev(uid, "task.done", a, task="T1", receipt=receipt)
        self.tev(uid, "ticket.submitted", a)
        return a


def pol(approvers=("owner",), count=1, not_=(), applies="all", independent=False):
    """A policy in canonical form (sorted lists)."""
    a = applies if isinstance(applies, str) else sorted(applies)
    return {
        "approvers": sorted(approvers),
        "count": count,
        "not": sorted(not_),
        "applies": a,
        "independent": independent,
    }


def _thaw(o: Any) -> Any:
    if hasattr(o, "items"):
        return {k: _thaw(v) for k, v in o.items()}
    if isinstance(o, tuple | list):
        return [_thaw(v) for v in o]
    return copy.copy(o)


def refused(w: World, log: str, typ: str, actor, **payload):
    """Append nothing; return the refusal code of ``admit`` (``None`` when the event would be admitted)."""
    st = w.state()
    from orch.model import admit

    res = admit(st, w.build_unappended(log, typ, actor, **payload), log=log)
    return getattr(res, "code", None)
