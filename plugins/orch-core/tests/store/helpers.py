"""A real workspace on a temp dir for the store tests: real keys, a file-tier workspace key through
``orch.custody.FileBackend``, person events signed with the device key, an injectable clock."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.custody import FileBackend
from orch.identity import certs, new_ulid
from orch.store import BackendSigner, Store
from orch.store.render import thaw
from tests.identity.helpers import Person

WS = "705d40abbb8c1c90354a1acaa94c935c"
T0 = 1_790_000_000  # 2026-09-21 ... the tests only need a fixed, plausible clock
GRANT_SECRET = b"s" * 32


def ts(epoch: int) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class Env:
    def __init__(self, tmp_path: Path, *, clock_start: int = T0) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "workspace"
        self.host_state = tmp_path / "host-state"
        self.clock = [clock_start]
        self.host_backend = FileBackend(tmp_path / "wsk")
        self.wsk_pub = self.host_backend.create("wsk")
        self.signer = BackendSigner(self.host_backend, "wsk")
        self.owner = Person()
        self.people: dict[str, Person] = {"owner": self.owner}
        self.store: Store | None = None
        self.grant_id: str | None = None

    # -- opening
    def open(self, **kw: Any) -> Store:
        kw.setdefault("host", self.signer)
        kw.setdefault("host_state_dir", self.host_state)
        kw.setdefault("clock", lambda: self.clock[0])
        kw.setdefault("workspace_name", "Acme")
        kw.setdefault("load", "all")  # the tests of the lazy path say load="lazy"
        self.store = Store.open(self.root, expected_workspace_id=WS, **kw)
        return self.store

    def other(self, **kw: Any) -> Store:
        """A second Store on the same workspace (another process, as far as the files can tell)."""
        kw.setdefault("host", self.signer)
        kw.setdefault("host_state_dir", self.host_state)
        kw.setdefault("clock", lambda: self.clock[0])
        kw.setdefault("load", "all")
        return Store.open(self.root, expected_workspace_id=WS, **kw)

    # -- person events
    def person_event(self, p: Person, log: str, typ: str, **payload: Any) -> dict[str, Any]:
        s = self.store
        assert s is not None
        e: dict[str, Any] = {
            "v": 2,
            "id": new_ulid(),
            "type": typ,
            "actor": {"kind": "person", "id": p.ref, "device": p.device},
            "auth": "passphrase",
            "hash_v": 1,
            "roster_v": s.state.workspace.roster_v,
            "based_on": s.log_head(log),
            **payload,
        }
        e["sig"] = crypto.b64u(p.dk_sign(canon.person_signing_bytes(WS, log, e)))
        return e

    def genesis(self) -> dict[str, Any]:
        o = self.owner
        deleg = certs.make_delegation(
            o.pk_pub, o.pk_sign, workspace_id=WS, wsk_pub=self.wsk_pub, client_hosted=False, issued_ms=T0 * 1000
        )
        e: dict[str, Any] = {
            "v": 2,
            "id": new_ulid(),
            "type": "workspace.created",
            "actor": {"kind": "person", "id": o.ref, "device": o.device},
            "auth": "passphrase",
            "hash_v": 1,
            "roster_v": 0,
            "based_on": None,
            "workspace_id": WS,
            "prefix": "DEMO",
            "host_id": "h_" + new_ulid(),
            "wsk_pub": crypto.b64u(self.wsk_pub),
            "owner": {"person": o.ref, "name": "Owner", "pk_pub": crypto.b64u(o.pk_pub)},
            "delegation": deleg,
            "device_cert": o.cert(created=T0 * 1000),
        }
        e["sig"] = crypto.b64u(o.dk_sign(canon.person_signing_bytes(WS, "workspace", e)))
        return e

    def grant_event(self, p: Person | None = None, hours: int = 8) -> dict[str, Any]:
        p = p or self.owner
        self.grant_id = "gr_" + new_ulid()
        issued = self.clock[0]
        return self.person_event(
            p,
            "workspace",
            "grant.issued",
            grant=self.grant_id,
            scope="all",
            verbs="agent",
            issued_at=ts(issued),
            hours=hours,
            expires_at=ts(issued + 3600 * hours),
            secret_hash=canon.grant_secret_hash(GRANT_SECRET),
            label="tests",
        )

    def add_device(self, p: Person | None = None) -> tuple[str, bytes]:
        """``device.added`` of a second device of ``p`` (signed by the first); returns (device ref, its dk_sig key)."""
        p = p or self.owner
        sig_key, kx_key = crypto.generate_private_key(), crypto.generate_private_key()
        sig_pub = crypto.public_bytes(sig_key)
        cert = certs.make_device_cert(
            p.pk_pub,
            p.pk_sign,
            dk_sig_pub=sig_pub,
            dk_kx_pub=crypto.public_bytes(kx_key),
            label_sealed=b"second",
            created_ms=T0 * 1000,
            expires_ms=None,
            scopes_max=["look", "decide", "operate", "type"],
        )
        ref = certs.device_ref(sig_pub)
        assert self.store is not None
        self.store.append(self.person_event(p, "workspace", "device.added", device=ref, cert=cert), log="workspace")
        return ref, sig_pub

    def revoke_device(self, ref: str, p: Person | None = None, reason: str = "compromised"):
        p = p or self.owner
        rev = certs.make_revocation(
            p.pk_pub, p.pk_sign, device_id_hex=ref[2:], revoked_ms=(self.clock[0] + 1) * 1000, reason=reason
        )
        assert self.store is not None
        return self.store.append(
            self.person_event(p, "workspace", "device.revoked", device=ref, reason=reason, revocation=rev),
            log="workspace",
        )

    def bootstrap(self, **kw: Any) -> Store:
        """An open store with the genesis and one grant for the owner."""
        s = self.open(**kw)
        s.append(self.genesis(), log="workspace")
        s.append(self.grant_event(), log="workspace")
        return s

    @property
    def agent(self) -> dict[str, Any]:
        return {
            "kind": "agent",
            "id": "claude-code",
            "session": "s_01J9ZP0000000000000000000S",
            "for": self.owner.ref,
            "grant": self.grant_id,
        }

    # -- ticket helpers
    def new_ticket(self, title: str = "A ticket", ticket_type: str = "feature") -> str:
        s = self.store
        assert s is not None
        r = s.create_ticket(actor=self.agent, ticket_type=ticket_type, title=title, owner=self.owner.ref)
        return r.event["seq"] and self._uid_of(r)

    def _uid_of(self, r: Any) -> str:
        s = self.store
        assert s is not None
        key = r.event["key"]
        return next(t.uid for t in r.state.tickets.values() if t.key == key)

    def base_rev(self, uid: str, sets: dict[str, Any], sections: dict[str, Any]) -> dict[str, str]:
        s = self.store
        assert s is not None
        v = s.state.tickets[uid]
        out = {}
        for path in sets:
            key = path[len("ticket.") :]
            out[path] = canon.value_hash(thaw(v.fields[key]))
        for sid in sections:
            out["body." + sid] = v.sections.get(sid, {"hash": canon.section_hash("")})["hash"]
        return out

    def update(self, uid: str, sets: dict[str, Any] | None = None, body: dict[str, str | None] | None = None):
        """An agent edit of ticket fields and/or sections, through ``Store.append``."""
        from orch.store.render import section_entry

        s = self.store
        assert s is not None
        sets = sets or {}
        body = body or {}
        sections = {sid: (None if t is None else section_entry(t)) for sid, t in body.items()}
        e: dict[str, Any] = {
            "type": "ticket.updated",
            "actor": self.agent,
            "base_rev": self.base_rev(uid, sets, sections),
        }
        if sets:
            e["set"] = sets
        if sections:
            e["sections"] = sections
        return s.append(e, log=uid, body=body)

    def fill(self, uid: str):
        """Everything the requirements gate needs: one criterion and the four sections."""
        return self.update(
            uid,
            {"ticket.acceptance": [{"id": "AC1", "text": "it works"}]},
            {"context": "ctx", "requirements": "req", "out_of_scope": "nothing"},
        )

    def approve(self, uid: str, gate: str = "requirements", p: Person | None = None):
        s = self.store
        assert s is not None
        g = s.state.tickets[uid].gates[gate]
        return s.append(
            self.person_event(
                p or self.owner, uid, "gate.approved", gate=gate, gate_gen=g.gen, hash=g.hash, policy_hash=g.policy_hash
            ),
            log=uid,
        )

    def close(self, uid: str):
        assert self.store is not None
        return self.store.append(self.person_event(self.owner, uid, "ticket.closed", resolution="other"), log=uid)

    def log(self, uid: str, text: str = "note"):
        assert self.store is not None
        return self.store.append({"type": "log.added", "actor": self.agent, "text": text}, log=uid)

    def base_rev_for(self, uid: str) -> dict[str, str]:
        return self.base_rev(uid, {"ticket.title": None}, {"context": None})

    def tick(self, seconds: int = 1) -> None:
        self.clock[0] += seconds

    # -- files
    def path(self, uid: str, name: str) -> Path:
        return self.root / "tickets" / uid / name

    def read_events(self, log: str) -> list[dict[str, Any]]:
        p = self.root / ("events/workspace.jsonl" if log == "workspace" else f"tickets/{log}/events.jsonl")
        return [canon.parse_event_line(line + b"\n") for line in p.read_bytes().splitlines()]


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file of a workspace except locks and the derived index, for before/after comparisons."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in ("lock", "index.sqlite") and ".tmp" not in p.name:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def deep(o: Any) -> Any:
    return copy.deepcopy(o)
