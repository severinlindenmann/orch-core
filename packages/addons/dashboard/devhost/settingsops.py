"""DEV ONLY, never shipped: workspace settings, members and grants on the P1 model (host contract v1.5).

Reads: `workspace.settings` (cap `settings/1`), `member.list` (devices with scopes and validity), `grant.list`
(`read_ext/2`: `revoked{by,at,seq}`, `session_ids`). Writes are prepared by `humanops.Builder` through the
`op_*` functions below: `settings.set`, `policy.set`, `member.add` (certificate as a file), `member.role`,
`member.remove`, `grant.revoke`. The visibility rules follow section 0.5 and the matrix: a repo's `path` and `remote`
go to owners and maintainers only, `grant.list` shows an owner every entry and anyone else their own.

The P1 model has no agent sessions: `sessions` is 0 and `session_ids` empty. Nothing here is a real-host guarantee.
"""

import base64
import json
import re
import time
from typing import Any

from orch import canon, repopaths
from orch.identity import check_member_added, person_ref
from orch.store.render import thaw

from orch_dashboard.errors import fail

PAGE = 32
DURATIONS = {"grant_hours": (1, 168), "claim_ttl_min": (15, 1440), "lease_ttl_min": (5, 1440)}
GATES = ("requirements", "plan", "verify", "code")
TOKENS = ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers")
ROLES = ("owner", "maintainer", "member", "viewer")
PERSON = re.compile(r"^p_[0-9a-f]{32}$")
GRANT = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
PAGE_TOKEN = re.compile(r"^st_[0-9]{1,6}$")


def invalid(message: str):
    return fail("invalid.input", message, exit=5)


def _ws_events(store) -> list[dict[str, Any]]:
    return [e for log, e in store.merged_events() if log == "workspace"]


def _last(store, kind: str) -> dict[str, Any] | None:
    found = [e for e in _ws_events(store) if e["type"] == kind]
    return {"seq": found[-1]["seq"], "at": found[-1]["at"], "by": found[-1]["actor"]["id"]} if found else None


# -- reads ---------------------------------------------------------------------------------------------------------
def settings(host, store, ws, person: str, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) - {"limit", "page"}:
        raise invalid("workspace.settings takes limit and page")
    limit = args.get("limit", PAGE)
    if type(limit) is not int or not 1 <= limit <= PAGE:
        raise invalid("limit is 1 to 32")
    start = 0
    if "page" in args:
        if not isinstance(args["page"], str) or not PAGE_TOKEN.fullmatch(args["page"]):
            raise fail("page.expired", "no such page", exit=5)
        start = int(args["page"][3:])
    role = ws.members[person].role
    names = sorted(ws.repos)
    shown = names[start : start + limit]
    repos = {}
    for name in shown:
        repo = ws.repos[name]
        entry: dict[str, Any] = {"default_branch": repo.get("default_branch")}
        if role in ("owner", "maintainer"):
            entry["path"] = repo["path"]
            entry["remote"] = repo.get("remote")
            entry["check"] = repopaths.observe(store.layout, repo["path"])  # as core serves a person at the keyboard
        repos[name] = entry
    set_by_event = {k for e in _ws_events(store) if e["type"] == "settings.changed" for k in e["set"]}
    nxt = f"st_{start + limit}" if start + limit < len(names) else None
    return {
        "settings": {**{k: ws.settings[k] for k in DURATIONS}, "repos": repos},
        "next_page": nxt,
        "gates": thaw(ws.policies),
        "defaults": [k for k in DURATIONS if k not in set_by_event],
        "settings_at": _last(store, "settings.changed"),
        "policy_at": _last(store, "policy.changed"),
        "sub_cursor": host.cursor,
    }


def members(ws, readers=None) -> dict[str, Any]:
    """`readers` (levels, required): the v1.9 `read_ext/3` device fields and `required_reader` (H12d-2)."""
    levels, required = readers if readers is not None else ({}, None)

    def ext(d):
        if readers is None:
            return {}
        return {"label": None, "paired_at": None, "relay": False, "cert_expires_at": None, "reader": levels.get(d.id)}

    out = {
        "members": [
            {
                "person": m.person,
                "name": m.name,
                "role": m.role,
                "devices": [
                    {
                        "id": d.id,
                        "auth": None,
                        "scopes": list(d.scopes),
                        "valid": d.expires_ms is None or d.expires_ms > time.time() * 1000,
                        **ext(d),
                    }
                    for d in ws.devices.values()
                    if d.person == m.person and not d.removed and not d.revoked
                ],
            }
            for m in ws.members.values()
        ]
    }
    if readers is not None:
        out["required_reader"] = required
    return out


def reader_list(ws, readers) -> dict[str, Any]:
    """`reader.list {}` (H12d-2): every current device; a device without a level has no record (0, no digest)."""
    levels, required = readers
    rows = []
    for d in ws.devices.values():
        if d.removed or d.revoked:
            continue
        level = levels.get(d.id)
        rows.append(
            {
                "device": d.id,
                "person": d.person,
                "dk_hash": "demo-not-a-real-dk-hash",
                "reader": level or 0,
                "level": level or 0,
                "digest": None if level is None else "sha256:" + "0" * 64,
                "decl": None if level is None else {"v": 1, "demo": "not-a-real-declaration"},
            }
        )
    return {"readers": sorted(rows, key=lambda r: r["device"]), "required_reader": required}


def grants(store, ws, person: str, args: dict[str, Any], ext: bool) -> dict[str, Any]:
    if set(args) - {"active"} or ("active" in args and type(args["active"]) is not bool):
        raise invalid("grant.list takes active")
    active = args.get("active", True)
    owner = ws.members[person].role == "owner"
    revoked = {e["grant"]: e for e in _ws_events(store) if e["type"] == "grant.revoked"}
    issued = {e["grant"]: e["issued_at"] for e in _ws_events(store) if e["type"] == "grant.issued"}
    rows = []
    for g in ws.grants.values():
        if not owner and g.person != person:
            continue  # a person-class owner sees every entry; anyone else their own
        if active and not g.active:  # not revoked and not expired at the store's clock
            continue
        row: dict[str, Any] = {
            "id": g.id,
            "person": g.person,
            "scope": g.scope,
            "verbs": g.verbs,
            "label": getattr(g, "label", None),  # H11c-2: the grant.issued label
            "issued_at": issued[g.id],
            "expires_at": g.expires_at,
            "revoked_at": revoked[g.id]["at"] if g.id in revoked else None,
            "sessions": 0,
        }
        if ext:
            row["session_ids"] = []
            if g.id in revoked:
                row["revoked"] = {
                    "by": revoked[g.id]["actor"]["id"],
                    "at": revoked[g.id]["at"],
                    "seq": revoked[g.id]["seq"],
                }
        rows.append(row)
    return {"grants": rows}


def _check_repos(layout, repos) -> None:
    """core's rules for `set.repos` (shape, name, a canonical remote) and the host's path checks (orch.repopaths)."""
    if not isinstance(repos, dict) or not 1 <= len(repos) <= 32:
        raise invalid("repos is one to 32 repos")
    for name, entry in repos.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise invalid("a repo name is lowercase letters, digits, dots, dashes")
        if entry is None:
            continue
        if not isinstance(entry, dict) or "path" not in entry or set(entry) - {"path", "remote", "default_branch"}:
            raise invalid("a repo is a path, with an optional remote and default_branch")
        if "remote" in entry:
            try:
                canon.check_repo_identity(entry["remote"])
            except canon.HashError as exc:
                raise invalid(str(exc)) from None
        try:
            repopaths.check_declared(layout, entry["path"])
        except repopaths.RepoPathError as exc:
            raise invalid(f"{name}: {exc}") from None


# -- writes (called by humanops.Builder) -----------------------------------------------------------------------------
def settings_set(builder, args):
    builder.exact(args, {"set"})
    given = args["set"]
    if not isinstance(given, dict) or not given or set(given) - set(DURATIONS) - {"repos"}:
        raise invalid("set is grant_hours, claim_ttl_min, lease_ttl_min or repos")
    for k, v in given.items():
        if k == "repos":
            _check_repos(builder.store.layout, v)
            continue
        low, high = DURATIONS[k]
        if type(v) is not int or not low <= v <= high:
            raise invalid(f"{k} is {low} to {high}")
    event = {"type": "settings.changed", "set": dict(given)}
    # `before` is labelled "current values according to the host, not signed" and only needs to be keys of `set`.
    before = {k: builder.store.state.workspace.settings.get(k) for k in given if k != "repos"}
    if "repos" in given:  # core's: the host's current entry of each named repo (null: none)
        before["repos"] = {n: thaw(builder.store.state.workspace.repos.get(n)) for n in given["repos"]}
    events = [builder.envelope(event, "workspace")]
    return events, "workspace", builder.payload_material(None, events, before=before, ticket=None)


def policy_set(builder, args):
    builder.exact(args, {"gates"}, {"ref"})
    if "ref" in args:
        raise fail("op.unsupported", "the dev stub sets workspace defaults only", exit=1)
    gates = args["gates"]
    if not isinstance(gates, dict) or not 1 <= len(gates) <= 4 or set(gates) - set(GATES):
        raise invalid("gates is 1 to 4 of requirements, plan, verify, code")
    for g, p in gates.items():
        _policy(g, p)
    events = [builder.envelope({"type": "policy.changed", "gates": json.loads(json.dumps(gates))}, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)


def _tokens(value, name, low, high):
    if (
        not isinstance(value, list)
        or not low <= len(value) <= high
        or not all(isinstance(t, str) and t in TOKENS for t in value)
        or value != sorted(set(value))
    ):
        raise invalid(f"{name} is {low} to {high} approver tokens, sorted and without repeats (canonical form)")


def _policy(gate, p):
    if not isinstance(p, dict) or set(p) != {"approvers", "count", "not", "applies", "independent"}:
        raise invalid("a policy has approvers, count, not, applies and independent")
    _tokens(p["approvers"], "approvers", 1, 8)
    _tokens(p["not"], "not", 0, 8)
    if type(p["count"]) is not int or not 1 <= p["count"] <= 9 or type(p["independent"]) is not bool:
        raise invalid("count is 1 to 9 and independent is true or false")
    a = p["applies"]
    if not (
        a in ("all", "off")
        or (isinstance(a, list) and 1 <= len(a) <= 16 and a == sorted(set(a)) and all(isinstance(t, str) for t in a))
    ):
        raise invalid("applies is all, off or a sorted list of ticket types")


def member_add(builder, args, files):
    builder.exact(args, {"name", "role", "pk", "cert"})
    name = builder.text(args["name"], "the name", required=True)
    if "\n" in name or len(name) > 64:
        raise invalid("the name is one line, at most 64 characters")
    if args["role"] not in ROLES:
        raise invalid("role is owner, maintainer, member or viewer")
    raw = files.get(args["cert"]) if isinstance(args["cert"], str) else None
    if raw is None:
        raise fail("blob.invalid", "the certificate file is missing", exit=5)
    try:
        cert = canon.loads_strict(raw)
        pk = base64.urlsafe_b64decode(str(args["pk"]) + "=")
    except Exception:  # noqa: BLE001 - any parse failure is the same refusal
        raise fail("parse.json", "the certificate is not strict JSON, or the key is not base64url", exit=6) from None
    event = {"type": "member.added", "person": person_ref(pk), "name": name, "role": args["role"],
             "pk_pub": args["pk"], "device_cert": cert}  # fmt: skip
    try:
        check_member_added(event)
    except Exception as exc:  # noqa: BLE001
        raise fail(
            "member.cert_invalid",
            f"the certificate does not belong to that key ({getattr(exc, 'code', 'bad')})",
            exit=5,
        ) from None
    events = [builder.envelope(event, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)


def member_role(builder, args):
    builder.exact(args, {"person", "role"})
    _member(builder, args["person"])
    if args["role"] not in ROLES:
        raise invalid("role is owner, maintainer, member or viewer")
    events = [builder.envelope({"type": "role.changed", "person": args["person"], "role": args["role"]}, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)


def member_remove(builder, args):
    builder.exact(args, {"person"})
    _member(builder, args["person"])
    events = [builder.envelope({"type": "member.removed", "person": args["person"]}, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)


DEVICE = re.compile(r"^d_[0-9a-f]{32}$")


def device_remove(builder, args):
    """The dev host's `device.remove`: the lockout guard of core for what it can see (an owner's last working device).
    The device that signs is known only to the signer, which refuses it again."""
    builder.exact(args, {"device"})
    device = args["device"]
    if not isinstance(device, str) or not DEVICE.fullmatch(device):
        raise invalid("device is a device id")
    ws = builder.store.state.workspace
    target = ws.devices.get(device)
    if target is None or target.removed:
        raise fail("not_found", "that device is not in this workspace (or it was removed already)", exit=2)
    holder = ws.members.get(target.person)
    working = [d for d in ws.devices.values() if d.person == target.person and not d.removed and not d.revoked]
    if holder is not None and holder.role == "owner" and len(working) <= 1:
        raise fail(
            "device.lockout",
            "This is the last working device of an owner. Removing it would lock that owner out for good. Pair or "
            "add another device first.",
            exit=3,
        )
    events = [builder.envelope({"type": "device.removed", "device": device}, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)


def _member(builder, person):
    if not isinstance(person, str) or not PERSON.fullmatch(person):
        raise invalid("person is a person id")
    if person not in builder.store.state.workspace.members:
        raise fail("not_found", f"{person} is not a member", exit=2)


def grant_revoke(builder, args):
    builder.exact(args, {"grant"}, {"reason"})
    gid = args["grant"]
    if not isinstance(gid, str) or not GRANT.fullmatch(gid):
        raise invalid("grant is a grant id")
    if gid not in builder.store.state.workspace.grants:
        raise fail("not_found", f"no grant {gid[:40]}", exit=2)
    event: dict[str, Any] = {"type": "grant.revoked", "grant": gid}
    if args.get("reason") is not None:
        event["reason"] = builder.text(args["reason"], "the reason")
    events = [builder.envelope(event, "workspace")]
    return events, "workspace", builder.payload_material(None, events, ticket=None)
