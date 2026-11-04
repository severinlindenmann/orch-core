"""Access changes that wait for a workspace owner (M2-8a, orch-core#1409, #1411).

A change that INCREASES access to a full workspace whose owner alias exists and is not the acting person becomes a pending
change: nothing reaches the access model until that owner approves (:func:`rules.needs_approval` decides, in one place). The
owner approves in the console with a signature of the browser's channel key over a digest that names the change, the
workspace, the exact body, the snapshot version it was based on and its expiry (:mod:`proof`); one proof is used once. A
change expires after 14 days and is never applied after that. A hub admin can override a pending change with a written
reason: it is applied, audited, and the owner sees a notice. Whatever the path, a change that would remove or demote the
owner alias's own admin access is refused (``owner_protected``).

Lock order (never the other way round): ``Access.lock`` first, then the changes database. Checks and the writes they guard
happen under ``Access.lock``, so another writer of the access model cannot slip in between.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ..access.store import ADMIN_GROUP, Access, AccessError, _grp, _hex, role_rank
from ..contract.canon import cj
from ..contract.hub import ROLES, Refused
from ..contract.phase4 import render_text
from ..custodial.engine import check_channel_proof
from . import proof as P
from . import rules as R
from .store import ChangeError, ChangesDB

_NO_END: Any = object()  # member_add without an end-date argument (the body then has no ``ends_ms``)
DAY_MS = 86_400_000
TTL_MS = 14 * DAY_MS
NOTICE_MS = 30 * DAY_MS  # how long "applied without your approval" stays on the owner's page
RECENT_MS = 14 * DAY_MS  # how long a finished change stays on the admin's list
REASON_MIN, REASON_MAX = 3, 200
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def new_id(now_ms: int) -> str:
    n = (now_ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def clean_reason(x: object) -> str:
    """The override reason: 3 to 200 characters, no control characters (it goes into the activity record)."""
    if type(x) is not str:
        raise ChangeError("malformed")
    s = x.strip()
    if not REASON_MIN <= len(s) <= REASON_MAX or any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise ChangeError("malformed")
    return s


def _canon(delta: list[dict[str, str]]) -> list[dict[str, str]]:
    """A delta in ONE order (person, then role), so two equal deltas compare equal whatever order they were built in."""
    return sorted(delta, key=lambda d: (d["user_id"], d["role"]))


class Changes:
    def __init__(
        self,
        path: str = ":memory:",
        *,
        connections: Any = None,  # orch_hub.connections.Connections: only connection changes need it
        access: Access,
        registry: Any,
        snapshots: Any,  # AccessSnapshots: the version a proposal is based on
        keys: Any = None,  # HubKeys + Channels: only the owner's proof needs them
        channels: Any = None,
        activity: Any = None,  # ActivityLog or None
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        import time

        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._access, self._reg, self._snaps = access, registry, snapshots
        self._keys, self._channels, self._activity = keys, channels, activity
        self._conns = connections
        self._db = ChangesDB(path)

    def close(self) -> None:
        self._db.close()

    # --- small reads -----------------------------------------------------------------------------------------------

    def _names(self) -> dict[str, str]:
        return {u["user_id"]: render_text(u["name"]) for u in self._access.users()}

    def _ws_name(self, ws: str) -> str | None:
        try:
            name = self._reg.display_name(ws)
        except KeyError:
            return None
        return render_text(name) if type(name) is str and name else None

    def _version(self, ws: str) -> int:
        try:
            return int(self._snaps.snapshot(ws, "full")["o"]["version"])
        except Refused, KeyError, TypeError, ValueError:
            raise ChangeError("unavailable") from None

    def _log(self, typ: str, ch: dict[str, Any], workspaces: list[str | None], **kw: Any) -> None:
        """One activity record per workspace (a record with no workspace when there is none). ``actor`` None = the hub."""
        if self._activity is None:
            return
        actor = kw.pop("actor", None)
        reason = kw.pop("reason", None)
        approved_by = kw.pop("approved_by", None)
        body = json.loads(ch["body"])
        detail: dict[str, Any] = {"change_id": ch["id"]}
        if reason is not None:
            detail["reason"] = reason
        if approved_by is not None:
            detail["approved_by"] = approved_by
        for ws in workspaces or [None]:
            self._activity.record(
                typ,
                actor_kind="person" if actor else "hub",
                actor_id=actor,
                subject_user=body.get("user_id"),
                workspace_id=ws,
                detail=detail,
            )

    def _ws_of(self, change_id: str) -> list[str | None]:
        return [a["workspace_id"] for a in self._db.approvals(change_id)]

    def _unowned_check(self, actor: str, kind: str, body: dict[str, Any]) -> None:
        """Fail closed for a FULL workspace the change reaches that has NO owner alias (a legacy or half-bound one): nobody
        can approve for it, so only a hub admin may change who reaches it (``owner_unknown`` for anyone else). It never
        becomes "applies at once" for whoever happens to ask."""
        for ws in R.reach_delta(self._access, self._reg, kind, body):
            if R.owner_of(self._reg, ws) is None and not self._access.is_hub_admin(actor):
                raise ChangeError("owner_unknown")

    # --- the one rule: a new grant or a new member ------------------------------------------------------------------

    def _validate_grant(self, ws: object, group: object, role: object) -> tuple[str, str, str]:
        ws, group = _hex(ws, "workspace_id"), _grp(group)
        if role not in ROLES:
            raise AccessError("malformed", f"role must be one of {', '.join(ROLES)}")
        if group == ADMIN_GROUP:
            raise AccessError("reserved_group", "hub-admin is implicit admin everywhere; it is not granted")
        if not any(g["name"] == group for g in self._access.groups()):
            raise AccessError("unknown_group")
        return ws, group, role  # type: ignore[return-value]

    def grant(
        self, actor: str, workspace_id: object, group: object, role: object, *, confirm: bool = False
    ) -> dict | None:
        """Grant ``group`` ``role`` in a workspace. ``None``: applied now. A dict: the pending change (nothing applied)."""
        ws, group, role = self._validate_grant(workspace_id, group, role)
        body = {"group": group, "role": role, "workspace_id": ws}
        with self._access.lock:
            self._unowned_check(actor, "grant", body)
            needs = R.needs_approval(self._access, self._reg, "grant", body, actor)
            if needs:
                return self._propose("grant", body, actor, needs)
            now = next((g["role"] for g in self._access.grants(ws) if g["group"] == group), None)
            if now is not None and role_rank(role) < role_rank(now):  # a lower role is a reduction
                self._guard(R.Edit(set_grant=(group, role)), [ws], confirm=confirm)
            self._access.grant(ws, group, role, actor=actor)
        return None

    def member_add(
        self, actor: str, group: object, user_id: object, *, ends_ms: object = _NO_END, confirm: bool = False
    ) -> dict | None:
        """Add ``user_id`` to ``group``, optionally with an end date (``ends_ms`` an int, or ``None`` = permanent). With an end
        date the body names it, so an owner's signed proof covers it. A later end (or none) in a group that reaches a full
        workspace is an increase and waits for its owner (R3); a shorter end is a reduction and runs the owner / last-admin
        guards as if the member were dropped now (R4)."""
        group = _grp(group)
        user_id = _hex(user_id, "user_id")
        body: dict[str, Any] = {"group": group, "user_id": user_id}
        if ends_ms is not _NO_END:
            body["ends_ms"] = self._access.check_end(ends_ms, group)
        with self._access.lock:
            if not any(g["name"] == group for g in self._access.groups()):
                raise AccessError("unknown_group")
            if not self._access.has_user(user_id):
                raise AccessError("unknown_user")
            self._unowned_check(actor, "member_add", body)
            needs = R.needs_approval(self._access, self._reg, "member_add", body, actor)
            if needs:
                return self._propose("member_add", body, actor, needs)
            if R.end_change(self._access, body) == "earlier":  # R4: as if the member were dropped now
                self._guard(
                    R.Edit(drop_member=(group, user_id)),
                    R.affected_by_group(self._access, self._reg, group),
                    confirm=confirm,
                )
            if "ends_ms" in body:
                self._access.add_member(group, user_id, actor=actor, ends_ms=body["ends_ms"])
            else:
                self._access.add_member(group, user_id, actor=actor)
        return None

    @contextmanager
    def reducing(self, op: str, *, confirm: bool = False, **kw: str) -> Iterator[None]:
        """Wrap a reduction (``revoke``, ``member_remove``, ``user_remove``, ``group_remove``): the owner protection and the
        last-admin guard run against the outcome, under ``Access.lock``, and the caller's write happens inside the block."""
        with self._access.lock:
            if op == "revoke":
                e, ws = R.Edit(drop_grant=kw["group"]), [kw["workspace_id"]]
            elif op == "member_remove":
                e, ws = (
                    R.Edit(drop_member=(kw["group"], kw["user_id"])),
                    R.affected_by_group(self._access, self._reg, kw["group"]),
                )
            elif op == "user_remove":
                e, ws = R.Edit(drop_user=kw["user_id"]), R.affected_by_user(self._access, self._reg, kw["user_id"])
            elif op == "group_remove":
                e, ws = R.Edit(drop_group=kw["group"]), R.affected_by_group(self._access, self._reg, kw["group"])
            else:
                raise ValueError("unknown reduction")
            self._guard(e, ws, confirm=confirm)
            yield

    def _guard(self, e: R.Edit, workspaces: list[str], *, confirm: bool) -> None:
        R.check_owner_protected(self._access, self._reg, e, workspaces)  # absolute
        if not confirm:
            R.check_last_admin(self._access, self._reg, e, workspaces)

    # --- propose ------------------------------------------------------------------------------------------------------

    def _propose(self, kind: str, body: dict[str, Any], actor: str, needs: list[tuple[str, str]]) -> dict[str, Any]:
        self.sweep()
        text, now = cj(body).decode(), self._now()
        for ch in self._db.changes("state='pending' AND kind=? AND proposer=? AND expires_ms>?", (kind, actor, now)):
            aps = self._db.approvals(ch["id"])
            if ch["body"] == text and [a["workspace_id"] for a in aps] == [ws for ws, _ in needs]:
                return self._propose_view(ch)  # the same request again: the one already waiting
        versions = {ws: self._version(ws) for ws, _ in needs}
        deltas = R.reach_delta(self._access, self._reg, kind, body)  # who the owners are really asked about
        cid = new_id(now)
        with self._db.tx() as db:
            db.execute(
                "INSERT INTO change(id, kind, body, proposer, created_ms, expires_ms, state, reach) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (
                    cid,
                    kind,
                    text,
                    actor,
                    now,
                    now + TTL_MS,
                    cj({ws: {"delta": _canon(d)} for ws, d in deltas.items()}).decode(),
                ),
            )
            for ws, owner in needs:
                db.execute(
                    "INSERT INTO approval(change_id, workspace_id, approver_user, base_snapshot_version, state, delta) "
                    "VALUES (?,?,?,?,'pending',?)",
                    (cid, ws, owner, versions[ws], cj(deltas.get(ws, [])).decode()),
                )
        ch = self._db.change(cid)
        assert ch is not None
        self._log("change.proposed", ch, [ws for ws, _ in needs], actor=actor)
        return self._propose_view(ch)

    def _propose_view(self, ch: dict[str, Any]) -> dict[str, Any]:
        names = self._names()
        return {
            "change_id": ch["id"],
            "waiting_for": [
                {
                    "workspace_id": a["workspace_id"],
                    "owner_name": names.get(R.owner_of(self._reg, a["workspace_id"]) or a["approver_user"]),
                }
                for a in self._db.approvals(ch["id"])
            ],
        }

    # --- connections between workspaces (M2-8b, #1414-#1417) -------------------------------------------------------------

    def _ws_hex(self, x: object) -> str:
        if type(x) is not str or not re.fullmatch(r"[0-9a-f]{32}", x):
            raise ChangeError("malformed")
        return x

    def propose_connection(self, actor: str, from_ws: object, to_ws: object) -> dict[str, Any]:
        """Ask that ``from_ws`` may send hand-offs to ``to_ws``. The proposer is a hub admin or the sender's owner alias; an
        eligible approver of the receiving workspace (never the proposer) must approve with a channel proof."""
        a, b = self._ws_hex(from_ws), self._ws_hex(to_ws)
        if a == b or self._conns is None or self._reg.mode(a) is None or self._reg.mode(b) is None:
            raise ChangeError("malformed")
        if not self._access.is_live(actor) or not (
            self._access.is_hub_admin(actor) or R.owner_of(self._reg, a) == actor
        ):
            raise ChangeError("forbidden_role")
        if self._conns.allowed(a, b):
            raise ChangeError("conflict")
        eligible = self.eligible_approvers(b, exclude=actor)
        # the normal single-owner case: the proposer owns the receiving workspace and nobody else could approve for it (or
        # they own both ends): they are the approver, so it applies at once as "the owner acting directly" (checked live,
        # right here at apply), audited, with no pending change
        if R.owner_of(self._reg, b) == actor and (R.owner_of(self._reg, a) == actor or not eligible):
            self._conns.add(a, b, approved_by=actor, via="approved")
            if self._activity is not None:
                self._activity.record("connection.allowed", actor_id=actor, workspace_id=b, detail={"direct": True})
            return {"direct": True}
        if not eligible:
            raise ChangeError("no_approver")  # nobody can approve: only a hub admin's override can allow it
        self.sweep()
        body = {"from": a, "op": "connection.allow", "to": b}
        text, now = cj(body).decode(), self._now()
        for ch in self._db.changes(
            "state='pending' AND kind='connection_allow' AND proposer=? AND expires_ms>?", (actor, now)
        ):
            if ch["body"] == text:
                return self._propose_view(ch)
        cid = new_id(now)
        with self._db.tx() as db:
            db.execute(
                "INSERT INTO change(id, kind, body, proposer, created_ms, expires_ms, state, reach) "
                "VALUES (?,?,?,?,?,?,'pending','{}')",
                (cid, "connection_allow", text, actor, now, now + TTL_MS),
            )
            db.execute(
                "INSERT INTO approval(change_id, workspace_id, approver_user, base_snapshot_version, state, delta) "
                "VALUES (?,?,?,0,'pending','[]')",
                (cid, b, eligible[0]),
            )
        ch = self._db.change(cid)
        assert ch is not None
        self._log("change.proposed", ch, [b], actor=actor)
        return self._propose_view(ch)

    def deny_connection(self, actor: str, from_ws: object, to_ws: object) -> bool:
        """Close ``from_ws -> to_ws`` at once (closing is always allowed): a hub admin, the sender's owner alias or an eligible
        approver of the receiving workspace. Pending proposals for the pair are withdrawn. True when a connection was removed."""
        a, b = self._ws_hex(from_ws), self._ws_hex(to_ws)
        if self._conns is None:
            raise ChangeError("unavailable")
        if not self._access.is_live(actor) or not (
            self._access.is_hub_admin(actor) or R.owner_of(self._reg, a) == actor or actor in self.eligible_approvers(b)
        ):
            raise ChangeError("forbidden_role")
        removed = self._conns.remove(a, b)
        text = cj({"from": a, "op": "connection.allow", "to": b}).decode()
        for ch in self._db.changes("state='pending' AND kind='connection_allow'"):
            if ch["body"] == text:
                self._cancel_row(ch, actor)
        if removed and self._activity is not None:
            self._activity.record("connection.denied", actor_id=actor, workspace_id=b)
        return removed

    def traffic_for(self, user_id: str) -> list[dict[str, Any]]:
        """Hand-off COUNTS per pair, last 30 days (Opus condition 13): a hub admin sees every pair; anyone else only pairs
        with a workspace they own or may approve for. Counts only: no content, no ticket, no person."""
        if self._conns is None:
            return []
        rows = self._conns.traffic()
        if self._access.is_hub_admin(user_id):
            mine = None
        else:
            mine = {
                ws
                for ws in self._reg.pins()
                if R.owner_of(self._reg, ws) == user_id or user_id in self.eligible_approvers(ws)
            }
        return [r for r in rows if mine is None or r["from"] in mine or r["to"] in mine]

    def _cancel_row(self, ch: dict[str, Any], actor: str) -> None:
        with self._db.tx() as db:
            db.execute(
                "UPDATE change SET state='cancelled', decided_ms=?, decided_by=? WHERE id=? AND state='pending'",
                (self._now(), actor, ch["id"]),
            )
        self._log("change.cancelled", ch, self._ws_of(ch["id"]), actor=actor)

    # --- views -------------------------------------------------------------------------------------------------------

    def eligible_approvers(self, to_ws: str, *, exclude: str | None = None) -> list[str]:
        """Who may approve a connection INTO ``to_ws`` (Opus condition 11): its owner alias, or a person holding an EXPLICIT
        workspace-level admin grant on it (a group granted role admin there). Membership of the hub-admin group alone does
        not count. A SELECTIVE recipient: only its owner alias. The proposer is never eligible for their own proposal."""
        out: set[str] = set()
        owner = R.owner_of(self._reg, to_ws)
        if owner is not None:
            out.add(owner)
        if R.is_full(self._reg, to_ws):
            for g in self._access.grants(to_ws):
                if g["role"] == "admin":
                    out.update(self._access.members(g["group"]))
        if exclude is not None:
            out.discard(exclude)
        return sorted(u for u in out if self._access.is_active(u))

    def _may_decide(self, ch: dict[str, Any], ap: dict[str, Any], user_id: str) -> bool:
        """May this person decide this approval row NOW? An access change: the workspace's current owner alias. A connection
        change: an eligible approver of the receiving workspace who is not the proposer."""
        if ch["kind"] == "connection_allow":
            return user_id in self.eligible_approvers(ap["workspace_id"], exclude=ch["proposer"])
        return R.owner_of(self._reg, ap["workspace_id"]) == user_id

    def _view(self, ch: dict[str, Any], names: dict[str, str], *, only_owner: str | None = None) -> dict[str, Any]:
        """One change as plain data. With ``only_owner`` the view holds just the approvals of workspaces that person owns
        NOW (an owner sees nothing about other workspaces)."""
        body = json.loads(ch["body"])
        rows = []
        for a in self._db.approvals(ch["id"]):
            owner = R.owner_of(self._reg, a["workspace_id"])
            if only_owner is not None and not self._may_decide(ch, a, only_owner):
                continue
            stale = False
            if ch["state"] == "pending" and a["state"] == "pending":
                try:
                    stale = self._version(a["workspace_id"]) != a["base_snapshot_version"]
                except ChangeError:
                    stale = False
            delta = json.loads(a["delta"])
            rows.append(
                {
                    "delta": delta,
                    "people": [{**d, "name": names.get(d["user_id"])} for d in delta],
                    "workspace_id": a["workspace_id"],
                    "workspace_name": self._ws_name(a["workspace_id"]),
                    "owner_user_id": owner or a["approver_user"],
                    "owner_name": names.get(owner or a["approver_user"]),
                    "state": a["state"],
                    "stale": stale,
                }
            )
        return {
            "id": ch["id"],
            "kind": ch["kind"],
            "state": ch["state"],
            "body": body,
            "group": body.get("group"),
            "role": body.get("role"),
            "from_name": self._ws_name(body["from"]) if "from" in body else None,  # connections: who may send to whom
            "to_name": self._ws_name(body["to"]) if "to" in body else None,
            "person_name": names.get(body["user_id"]) if "user_id" in body else None,
            "proposer": {"user_id": ch["proposer"], "name": names.get(ch["proposer"])},
            "created_ms": ch["created_ms"],
            "expires_ms": ch["expires_ms"],
            "decided_ms": ch["decided_ms"],
            "decided_by": names.get(ch["decided_by"]) if ch["decided_by"] else None,
            "reason": render_text(ch["reason"]) if ch["reason"] else None,
            "approvals": rows,
        }

    def for_owner(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        """What the session's person may decide, and the overrides on their workspaces. Scoped by the person alone."""
        self.sweep()
        names, now = self._names(), self._now()
        pending, notices = [], []
        for ch in self._db.changes("state='pending'"):
            if any(a["state"] == "pending" and self._may_decide(ch, a, user_id) for a in self._db.approvals(ch["id"])):
                pending.append(self._view(ch, names, only_owner=user_id))
        for ch in self._db.changes("state='overridden' AND decided_ms>?", (now - NOTICE_MS,)):
            view = self._view(ch, names, only_owner=user_id)
            if view["approvals"]:
                notices.append(view)
        return {"pending": pending, "notices": notices}

    def for_admin(self) -> list[dict[str, Any]]:
        """Waiting changes, then the ones finished in the last 14 days (newest first)."""
        self.sweep()
        names, now = self._names(), self._now()
        rows = self._db.changes("state='pending' OR decided_ms>?", (now - RECENT_MS,))
        rows.sort(key=lambda c: (c["state"] != "pending", -c["created_ms"]))
        return [self._view(ch, names) for ch in rows[:200]]

    # --- the owner's decision ------------------------------------------------------------------------------------------

    def _mine_pending(self, change_id: object, user_id: str, workspace_id: object) -> tuple[dict, dict]:
        """The change and the approval row of the CURRENT owner. Anyone else, and anything that does not exist, is
        ``unknown_change``: nobody learns which ids exist."""
        ch = self._db.change(change_id) if type(change_id) is str and ULID.fullmatch(change_id) else None
        if ch is None:
            raise ChangeError("unknown_change")
        mine = [a for a in self._db.approvals(ch["id"]) if self._may_decide(ch, a, user_id)]
        if workspace_id is not None:
            if type(workspace_id) is not str:
                raise ChangeError("malformed")
            mine = [a for a in mine if a["workspace_id"] == workspace_id]
        if not mine:
            raise ChangeError("unknown_change")
        if ch["state"] != "pending":
            raise ChangeError("expired" if ch["state"] == "expired" else "not_pending")
        if self._now() >= ch["expires_ms"]:
            self._expire(ch)
            raise ChangeError("expired")
        open_ = [a for a in mine if a["state"] == "pending"]
        if not open_:
            raise ChangeError("not_pending")
        if len(open_) > 1:
            raise ChangeError(
                "malformed", "workspace_id"
            )  # the person owns several of the affected workspaces: say which
        return ch, open_[0]

    def _context(self, ch: dict[str, Any], ap: dict[str, Any]) -> dict[str, Any]:
        return P.context(
            ch["id"],
            ap["workspace_id"],
            json.loads(ch["body"]),
            ap["base_snapshot_version"],
            ch["expires_ms"],
            json.loads(ap["delta"]),
        )

    def _delta_same(self, ch: dict[str, Any], ap: dict[str, Any]) -> bool:
        """The people the owner was shown are exactly who the change would add or raise NOW (recomputed, not remembered)."""
        now = R.reach_delta(self._access, self._reg, ch["kind"], json.loads(ch["body"])).get(ap["workspace_id"], [])
        return cj(_canon(now)) == cj(_canon(json.loads(ap["delta"])))

    def _proposer_stands(self, ch: dict[str, Any]) -> bool:
        """The proposer is still a live person with the standing that let them propose: a hub admin for an access change; a
        hub admin or the SENDER workspace's owner alias for a connection."""
        p = ch["proposer"]
        if not self._access.has_user(p) or not self._access.is_live(p):
            return False
        if self._access.is_hub_admin(p):
            return True
        if ch["kind"] == "connection_allow":
            return R.owner_of(self._reg, json.loads(ch["body"])["from"]) == p
        return False

    def _moved(self, ch: dict[str, Any], ap: dict[str, Any]) -> bool:
        """Has what the approver was shown moved? An access change: the workspace's snapshot version or the people it
        reaches. A connection: either end left the hub, or it already exists (nothing is left to approve)."""
        if ch["kind"] == "connection_allow":
            b = json.loads(ch["body"])
            return (
                self._conns is None
                or self._reg.mode(b["from"]) is None
                or self._reg.mode(b["to"]) is None
                or self._conns.allowed(b["from"], b["to"])
            )
        return self._version(ap["workspace_id"]) != ap["base_snapshot_version"] or not self._delta_same(ch, ap)

    def _check_fresh(self, ch: dict[str, Any], ap: dict[str, Any]) -> None:
        if self._moved(ch, ap):
            self._stale(ch["id"], ap["workspace_id"])
            raise ChangeError("stale_change")

    def _stale(self, change_id: str, ws: str, db: Any = None) -> None:
        """Access to the workspace moved since the proposal: the approval is stale and the change is over (the proposer
        proposes again). With ``db`` the writes join that transaction (the caller records the activity)."""
        now = self._now()

        def go(c: Any) -> bool:
            c.execute(
                "UPDATE approval SET state='stale', decided_ms=? WHERE change_id=? AND workspace_id=? AND state='pending'",
                (now, change_id, ws),
            )
            return (
                c.execute(
                    "UPDATE change SET state='rejected', decided_ms=?, reason='stale_change' WHERE id=? AND state='pending'",
                    (now, change_id),
                ).rowcount
                == 1
            )

        if db is not None:
            go(db)
            return
        with self._db.tx() as c:
            done = go(c)
        ch = self._db.change(change_id)
        if done and ch is not None:
            self._log("change.rejected", ch, [ws], reason="stale_change")

    def prepare_approval(self, change_id: object, user_id: str, workspace_id: object = None) -> dict[str, Any]:
        """What the owner is asked to sign: the context (the console recomputes the digest from it) and the digest."""
        self.sweep()
        ch, ap = self._mine_pending(change_id, user_id, workspace_id)
        self._check_fresh(ch, ap)
        ctx = self._context(ch, ap)
        return {
            "ctx": ctx,
            "digest": P.digest(ctx).hex(),
            "workspace_id": ap["workspace_id"],
            "expires_ms": ch["expires_ms"],
            "change": self._view(ch, self._names(), only_owner=user_id),
        }

    def approve(
        self, change_id: object, user_id: str, channel_id: object, proof_b64: object, workspace_id: object = None
    ) -> dict[str, Any]:
        """The owner's signed approval of one workspace of a change. Applies the change when every affected workspace has
        been approved. Refuses (writing nothing, except the stale / expired marks) unless: the change is pending and not
        expired, the person is the workspace's CURRENT owner alias, access to the workspace has not moved since the
        proposal, the proof verifies over the digest for exactly this change and workspace, and the proof was never used."""
        if type(channel_id) is not str or type(proof_b64) is not str or self._keys is None or self._channels is None:
            raise ChangeError("malformed" if self._keys is not None else "unavailable")
        self.sweep()
        ch, ap = self._mine_pending(change_id, user_id, workspace_id)
        ws = ap["workspace_id"]
        self._check_fresh(ch, ap)
        sig = check_channel_proof(
            keys=self._keys,
            channels=self._channels,
            user_id=user_id,
            channel_id=channel_id,
            digest=P.digest(self._context(ch, ap)),
            proof=proof_b64,
            now_ms=self._now(),
        )
        used = hashlib.sha256(sig).hexdigest()
        failure: str | None = None
        result: dict[str, Any] = {}
        logged: list[tuple[str, dict[str, Any], list[str | None], dict[str, Any]]] = []
        with self._access.lock, self._db.tx() as db:
            ch = self._db.change(ch["id"]) or ch
            ap = next((a for a in self._db.approvals(ch["id"]) if a["workspace_id"] == ws), ap)
            now = self._now()
            if ch["state"] != "pending" or ap["state"] != "pending":
                raise ChangeError("not_pending")
            if now >= ch["expires_ms"]:
                db.execute(
                    "UPDATE change SET state='expired', decided_ms=? WHERE id=? AND state='pending'", (now, ch["id"])
                )
                logged.append(("change.expired", ch, self._ws_of(ch["id"]), {}))
                failure = "expired"
            elif not self._may_decide(ch, ap, user_id):
                raise ChangeError("unknown_change")
            elif self._moved(ch, ap):
                self._stale(ch["id"], ws, db)
                logged.append(("change.rejected", ch, [ws], {"reason": "stale_change"}))
                failure = "stale_change"
            else:
                if db.execute("SELECT 1 FROM approval WHERE proof_digest=?", (used,)).fetchone():
                    raise ChangeError("replay")
                db.execute(
                    "UPDATE approval SET state='approved', approver_user=?, decided_ms=?, proof_digest=? "
                    "WHERE change_id=? AND workspace_id=? AND state='pending'",
                    (user_id, now, used, ch["id"], ws),
                )
                logged.append(("change.approved", ch, [ws], {"actor": user_id, "approved_by": user_id}))
                aps = self._db.approvals(ch["id"])
                if all(a["state"] == "approved" for a in aps):
                    failure = self._finish_approved(db, ch, aps)
                    result = {"state": "rejected" if failure else "applied", "remaining": 0}
                    if failure == "expired":
                        logged.append(("change.expired", ch, self._ws_of(ch["id"]), {}))
                    elif failure:
                        logged.append(("change.rejected", ch, self._ws_of(ch["id"]), {"reason": failure}))
                else:
                    result = {"state": "pending", "remaining": sum(1 for a in aps if a["state"] != "approved")}
        for typ, c, workspaces, kw in logged:
            self._log(typ, c, workspaces, **kw)
        if failure:
            raise ChangeError(failure)
        return result

    def _finish_approved(self, db: Any, ch: dict[str, Any], aps: list[dict[str, Any]]) -> str | None:
        """Every workspace said yes: re-check the whole change and apply it. Returns the failure code (the change is then
        rejected and the caller commits that) or None once applied. Inside the caller's transaction and ``Access.lock``."""
        now, body = self._now(), json.loads(ch["body"])
        if now >= ch["expires_ms"]:  # an expired change is never applied, whatever happened before
            db.execute(
                "UPDATE change SET state='expired', decided_ms=? WHERE id=? AND state='pending'", (now, ch["id"])
            )
            return "expired"

        def reject(code: str) -> str:
            db.execute(
                "UPDATE change SET state='rejected', decided_ms=?, reason=? WHERE id=? AND state='pending'",
                (now, code, ch["id"]),
            )
            return code

        # the access to every affected workspace is exactly what each owner saw (a later approval does not cover an
        # earlier one that went stale), and nobody else needs to approve now that did not have to when it was proposed
        for a in aps:
            if self._moved(ch, a):
                db.execute(
                    "UPDATE approval SET state='stale' WHERE change_id=? AND workspace_id=?",
                    (ch["id"], a["workspace_id"]),
                )
                return reject("stale_change")
        # the proposer must still stand where they stood when they proposed: a live hub admin (a disabled, unrecovered or
        # demoted proposer voids the change)
        proposer = ch["proposer"]
        if not self._proposer_stands(ch):
            return reject("proposer_gone")
        approved = {a["workspace_id"]: a["approver_user"] for a in aps if a["state"] == "approved"}
        try:
            self._apply(ch, body, actor=proposer, approved=approved)
        except ChangeError as e:
            return reject(e.code)
        except AccessError as e:
            return reject(e.code)
        db.execute(
            "UPDATE change SET state='applied', decided_ms=?, decided_by=NULL WHERE id=? AND state='pending'",
            (now, ch["id"]),
        )
        return None

    def _check_reach(
        self, ch: dict[str, Any], body: dict[str, Any], actor: str, approved: dict[str, str] | None
    ) -> None:
        """The change reaches exactly what was proposed, judged against ownership NOW (never against what it was): every
        workspace it reaches must be in the proposal's recorded reach with the SAME people (else ``stale_change``), an
        owner-less one only ever for a hub admin (``owner_unknown``), and (``approved`` given: an owner approval, not an
        override) every one owned by someone other than the proposer must carry that owner's approval. The proposer's own
        workspaces are exempt only while the proposer is STILL their owner."""
        stored = json.loads(ch["reach"])
        override = approved is None  # an override reaches the owners it finds NOW (and tells them), but never an orphan
        for ws, delta in R.reach_delta(self._access, self._reg, ch["kind"], body).items():
            owner = R.owner_of(self._reg, ws)
            known = ws in stored and cj(_canon(stored[ws]["delta"])) == cj(_canon(delta))  # the same people as proposed
            if owner is None:  # never matches the proposer exemption: hub-admin only, and only exactly as proposed
                if not known:
                    raise ChangeError("stale_change")
                if not self._access.is_hub_admin(actor):
                    raise ChangeError("owner_unknown")
            elif not override:
                # who approved must be who owns it NOW (an approval follows the person, not the workspace)
                if not known or (owner != ch["proposer"] and approved.get(ws) != owner):
                    raise ChangeError("stale_change")

    def _apply(self, ch: dict[str, Any], body: dict[str, Any], *, actor: str, approved: dict[str, str] | None) -> None:
        """Make the change. Never after its expiry, and always with the owner protection checked against the state NOW (an
        approved or overridden grant is applied as a role to SET, which could be lower than what the group holds by then)."""
        if self._now() >= ch["expires_ms"]:
            raise ChangeError("expired")
        kind = ch["kind"]
        if kind == "connection_allow":
            self._apply_connection(ch, body, actor, approved)
            return
        self._check_reach(ch, body, actor, approved)  # new reach and orphan workspaces, judged now
        if kind == "grant":
            self._guard_owner(R.Edit(set_grant=(body["group"], body["role"])), [body["workspace_id"]])
            self._access.grant(body["workspace_id"], body["group"], body["role"], actor=actor)
        elif kind == "member_add":
            if "ends_ms" in body:  # a pending change whose end has passed by now is refused, never applied
                if body["ends_ms"] is not None and body["ends_ms"] <= self._now():
                    raise ChangeError("expired")
                self._access.add_member(body["group"], body["user_id"], actor=actor, ends_ms=body["ends_ms"])
            else:
                self._access.add_member(body["group"], body["user_id"], actor=actor)
        else:
            raise ChangeError("malformed")

    def _apply_connection(
        self, ch: dict[str, Any], body: dict[str, Any], actor: str, approved: dict[str, str] | None
    ) -> None:
        """Make the connection. An owner approval: the approver must be an eligible approver of the receiving workspace NOW
        and not the proposer. An override (``approved`` is None): a hub admin, audited by the caller."""
        if self._conns is None:
            raise ChangeError("unavailable")
        a, b = body["from"], body["to"]
        approver = None
        if approved is not None:
            approver = approved.get(b)
            if (
                approver is None
                or approver == ch["proposer"]
                or approver not in self.eligible_approvers(b, exclude=ch["proposer"])
            ):
                raise ChangeError("stale_change")
        self._conns.add(a, b, approved_by=approver or actor, via="approved" if approved is not None else "override")
        if self._activity is not None:
            self._activity.record(
                "connection.allowed",
                actor_id=approver or actor,
                workspace_id=b,
                detail={"approved_by": approver} if approver else None,
            )

    def _guard_owner(self, e: R.Edit, workspaces: list[str]) -> None:
        R.check_owner_protected(self._access, self._reg, e, workspaces)

    def reject(self, change_id: object, user_id: str, workspace_id: object = None) -> None:
        """The owner says no: the change is over. Needs a registered channel (nothing to sign)."""
        self.sweep()
        ch, ap = self._mine_pending(change_id, user_id, workspace_id)
        if self._channels is not None and not any(c["status"] == "active" for c in self._channels.channels(user_id)):
            raise ChangeError("no_channel")
        now = self._now()
        with self._db.tx() as db:
            db.execute(
                "UPDATE approval SET state='rejected', approver_user=?, decided_ms=? "
                "WHERE change_id=? AND workspace_id=? AND state='pending'",
                (user_id, now, ch["id"], ap["workspace_id"]),
            )
            if (
                db.execute(
                    "UPDATE change SET state='rejected', decided_ms=?, decided_by=?, reason='rejected by the owner' "
                    "WHERE id=? AND state='pending'",
                    (now, user_id, ch["id"]),
                ).rowcount
                != 1
            ):
                raise ChangeError("not_pending")
        self._log("change.rejected", ch, [ap["workspace_id"]], actor=user_id, reason="rejected by the owner")

    # --- override, cancel, expiry, void ---------------------------------------------------------------------------------

    def override(self, change_id: object, admin: str, reason: object) -> None:
        """A hub admin applies a pending change without the owner, with a written reason. Audited BEFORE it is applied (an
        extra record is possible, a missing one never); the owner sees a notice; the owner protection still holds."""
        reason = clean_reason(reason)
        self.sweep()
        with self._access.lock:
            ch = self._db.change(change_id) if type(change_id) is str and ULID.fullmatch(change_id) else None
            if ch is None:
                raise ChangeError("unknown_change")
            if ch["state"] != "pending":
                raise ChangeError("expired" if ch["state"] == "expired" else "not_pending")
            if self._now() >= ch["expires_ms"]:
                self._expire(ch)
                raise ChangeError("expired")
            body = json.loads(ch["body"])
            if ch["kind"] == "grant":
                self._guard_owner(R.Edit(set_grant=(body["group"], body["role"])), [body["workspace_id"]])
                self._validate_grant(body["workspace_id"], body["group"], body["role"])
            elif ch["kind"] == "connection_allow":
                if self._conns is None or self._reg.mode(body["from"]) is None or self._reg.mode(body["to"]) is None:
                    raise ChangeError("stale_change")
            elif not self._access.has_user(body["user_id"]):
                raise AccessError("unknown_user")
            # what the change does to access NOW, not what it did when it was proposed: workspaces bound or granted since
            # then are reached too, and every owner it reaches is told (condition 9). Everything below happens under
            # Access.lock, after the audit record (an extra record is possible, a missing one never), inside ONE
            # transaction with the compare-and-swap: a lost race leaves no notice behind.
            reached = self._reach_now(ch, body)
            self._log(
                "change.overridden",
                ch,
                sorted(set(self._ws_of(ch["id"])) | set(reached)),
                actor=admin,
                reason=reason,
            )
            with self._db.tx() as db:
                if (
                    db.execute(
                        "UPDATE change SET state='overridden', decided_ms=?, decided_by=?, reason=? "
                        "WHERE id=? AND state='pending'",
                        (self._now(), admin, reason, ch["id"]),
                    ).rowcount
                    != 1
                ):
                    raise ChangeError("not_pending")
                # the notice is computed from the reach NOW, inside this transaction and right before the apply (all under
                # Access.lock), so what the owners are told is exactly what is applied
                for ws, delta in self._reach_now(ch, body).items():
                    self._owner_notice_row(db, ch["id"], ws, delta)
                db.execute("UPDATE approval SET state='overridden' WHERE change_id=? AND state='pending'", (ch["id"],))
                self._apply(ch, body, actor=admin, approved=None)

    def _reach_now(self, ch: dict[str, Any], body: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        """Who an access change would add or raise, per workspace, NOW. A connection change reaches no one's access."""
        if ch["kind"] == "connection_allow":
            return {}
        return R.reach_delta(self._access, self._reg, ch["kind"], body)

    def _owner_notice_row(self, db: Any, change_id: str, ws: str, delta: list[dict[str, str]]) -> None:
        """Make sure the owner of ``ws`` has an approval row on this change that carries the people who REALLY gain access
        (the notice reads these rows): a new row for a workspace the proposal did not name, the delta refreshed on the
        rest. Inside the caller's transaction; the rows end as ``overridden``, never ``pending``, so no approve button can
        appear on a change that is already applied."""
        owner = R.owner_of(self._reg, ws)
        if owner is None:
            return  # nobody to tell (the activity record names the workspace)
        text = cj(delta).decode()
        if db.execute("SELECT 1 FROM approval WHERE change_id=? AND workspace_id=?", (change_id, ws)).fetchone():
            db.execute("UPDATE approval SET delta=? WHERE change_id=? AND workspace_id=?", (text, change_id, ws))
        else:
            db.execute(
                "INSERT INTO approval(change_id, workspace_id, approver_user, base_snapshot_version, state, delta) "
                "VALUES (?,?,?,?,'overridden',?)",
                (change_id, ws, owner, self._version(ws), text),
            )

    def cancel(self, change_id: object, actor: str) -> None:
        """The proposer, or a hub admin, withdraws a pending change."""
        self.sweep()
        ch = self._db.change(change_id) if type(change_id) is str and ULID.fullmatch(change_id) else None
        if ch is None or (ch["proposer"] != actor and not self._access.is_hub_admin(actor)):
            raise ChangeError("unknown_change")
        if ch["state"] != "pending":
            raise ChangeError("expired" if ch["state"] == "expired" else "not_pending")
        with self._db.tx() as db:
            if (
                db.execute(
                    "UPDATE change SET state='cancelled', decided_ms=?, decided_by=? WHERE id=? AND state='pending'",
                    (self._now(), actor, ch["id"]),
                ).rowcount
                != 1
            ):
                raise ChangeError("not_pending")
        self._log("change.cancelled", ch, self._ws_of(ch["id"]), actor=actor)

    def _expire(self, ch: dict[str, Any]) -> bool:
        with self._db.tx() as db:
            done = (
                db.execute(
                    "UPDATE change SET state='expired', decided_ms=? WHERE id=? AND state='pending'",
                    (self._now(), ch["id"]),
                ).rowcount
                == 1
            )
        if done:
            self._log("change.expired", ch, self._ws_of(ch["id"]))
        return done

    def sweep(self) -> int:
        """Expire every pending change past its time (each writes ``change.expired``). Lazy callers use it too."""
        n = 0
        for ch in self._db.changes("state='pending' AND expires_ms<=?", (self._now(),)):
            n += self._expire(ch)
        return n

    def void_for_user(self, user_id: str) -> dict[str, int]:
        """A person is going away (the user-disable slice calls this): their pending proposals are cancelled and the
        approvals that wait on them are rejected (nobody is left to answer), so nothing waits for a person who cannot."""
        self.sweep()
        out = {"cancelled": 0, "rejected": 0}
        now = self._now()
        for ch in self._db.changes("state='pending'"):
            aps = self._db.approvals(ch["id"])
            waiting = [
                a
                for a in aps
                if a["state"] == "pending" and user_id in (a["approver_user"], R.owner_of(self._reg, a["workspace_id"]))
            ]
            if ch["proposer"] == user_id:
                new, reason, key = "cancelled", None, "cancelled"
            elif waiting:
                new, reason, key = "rejected", "the owner is no longer available", "rejected"
            else:
                continue
            with self._db.tx() as db:
                if (
                    db.execute(
                        "UPDATE change SET state=?, decided_ms=?, reason=? WHERE id=? AND state='pending'",
                        (new, now, reason, ch["id"]),
                    ).rowcount
                    != 1
                ):
                    continue
                for a in waiting:
                    db.execute(
                        "UPDATE approval SET state='rejected', decided_ms=? WHERE change_id=? AND workspace_id=?",
                        (now, ch["id"], a["workspace_id"]),
                    )
            out[key] += 1
            self._log(
                "change.cancelled" if new == "cancelled" else "change.rejected",
                ch,
                [a["workspace_id"] for a in aps],
                reason=reason,
            )
        return out
