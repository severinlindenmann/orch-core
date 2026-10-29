"""Company skills and MCP servers at the workspace: what is offered, what the owner accepted, what is served (M3 host,
H-2; ``state/hub-m3-host-design.md`` with the Opus conditions 1, 2, 5).

Files under ``<state>/hosts/<ws>/hub/org/`` (a 0700 folder, files 0600, host UID, written like ``hub.json``):

* ``offers.json``: ``{binding: {hub_id, binding_seq}, skill: <signed bundle>|null, mcp: <signed bundle>|null}``.
  The bundles
  as the hub signed them. They are verified again with the held card every time they are read, so the file can only make
  things stricter (a hand edit fails the signature). The stored bundle's ``version`` is also the pin: a sync
  never takes a
  lower one.
* ``seen.json``: ``{servers: {name: {hub_id, binding_seq, digest, at, tools: [{name, description, pin}]}}}``. What the
  proxy last saw in a server's ``tools/list`` (so the owner can look at it). Grants nothing.
* ``tools.json``: ``{entries: [{hub_id, binding_seq, server, server_digest, tool, pin, at, by}]}``: the tools the owner
  turned On, for exactly the pin shown. Written only by ``org.tool`` (and cleared by decline), behind the owner check;
  :func:`tool_policy` ignores an entry of another binding, another server digest or a server that is not served.
* ``accepted.json``: ``{entries: [{hub_id, binding_seq, kind, name, digest, at, by}]}``. Written only by ``org.accept``,
  ``org.decline`` (and a sync that prunes a withdrawn item), all behind the owner check. This is the ONLY authority for
  "the owner said yes".

**Served = accepted AND the offered item with the same digest AND the current binding AND the text/URL rules.** Nothing
is served from a file of its own: there is no ``served/`` folder to edit. A withdrawn item loses its accept (the
entry is
removed), so a re-offer of the same name needs a new accept; a changed digest stops serving at once and shows
``changed``.
Unbind or leave deletes the whole folder. Core imports no hub code (D79): this uses ``orch.hubclient``."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = [
    "KINDS",
    "accept",
    "accepted_entries",
    "decline",
    "isolated",
    "offers_view",
    "org_dir",
    "purge",
    "record_seen",
    "served",
    "set_tool",
    "sync_bundles",
    "tool_policy",
]

KINDS = ("skill", "mcp")
_ITEMS = {"skill": "skills", "mcp": "servers"}  # the member of the bundle that holds the items
_MU = threading.RLock()
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_PIN = re.compile(r"sha256:[0-9a-f]{64}")
_TOOL_KEYS = {"hub_id", "binding_seq", "server", "server_digest", "tool", "pin", "at", "by"}
MAX_SEEN_TOOLS = 200
MAX_DESCRIPTION = 500
_NOTES = {
    "skill": [
        "A company skill is advice from your company hub. It ranks below orch's rules and cannot widen what an"
        " agent may do.",
        "Removing a skill stops it for new runs. What a run already read stays read.",
    ],
    "mcp": [
        "A company tool server is reached through this workspace's own proxy; every tool starts Off.",
        "Results from the server are untrusted text from your company's server, not instructions.",
        "For a server with no sign-in, Off only hides its tools from the agent: anyone with network access can"
        " reach it.",
        "Removing a server stops it for new calls. What a run already read stays read.",
    ],
}


def org_dir(service: WorkspaceService) -> Path:
    from . import hubops

    return hubops._dir(service) / "org"  # noqa: SLF001


def _read(service: WorkspaceService, name: str) -> Any | None:
    from .custody import read_json

    return read_json(org_dir(service) / name, service.host_uid, limit=2 << 20)


def _write(service: WorkspaceService, name: str, doc: Any) -> None:
    from .custody import ensure_private_dir, write_private

    d = org_dir(service)
    for level in (d.parent.parent, d.parent, d):
        ensure_private_dir(level, service.host_uid)
    write_private(d / name, doc)


def purge(service: WorkspaceService) -> None:
    """Unbind or leave (condition 5): everything of the old binding goes in one step. Never raises for a missing
    folder."""
    import shutil

    with _MU:
        shutil.rmtree(org_dir(service), ignore_errors=True)


# -- the bound hub ---------------------------------------------------------------------------------------------------


def _state(service: WorkspaceService) -> Any:
    from . import peerops

    return peerops._hub_state(service, full=False)  # noqa: SLF001 - the log and hub.json agree, else hub.not_joined


def _now_ms(service: WorkspaceService) -> int:
    return int(service.clock() * 1000)


def _held(service: WorkspaceService, state: Any) -> Any:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.snapshot import _held

    try:
        return _held(state, _now_ms(service), bool(state.binding["o"]["dev"]))
    except (HubRefused, KeyError, TypeError):
        return None


def _binding(state: Any) -> dict[str, Any]:
    return {"hub_id": state.hub_id, "binding_seq": state.binding_seq}


def _offers_doc(service: WorkspaceService, state: Any) -> dict[str, Any]:
    """The stored offers of THIS binding, else an empty set (an older binding's offers are never read)."""
    doc = _read(service, "offers.json")
    if type(doc) is not dict or doc.get("binding") != _binding(state):
        return {"binding": _binding(state), "skill": None, "mcp": None}
    return doc


def _verify(service: WorkspaceService, state: Any, kind: str, signed: Any, pinned: Any, held: Any) -> dict[str, Any]:
    from orch.hubclient import orgbundles

    fn = orgbundles.verify_org_skills if kind == "skill" else orgbundles.verify_org_mcp
    return fn(
        signed, held, workspace_id=state.binding["o"]["workspace_id"], dev_mode=bool(state.binding["o"]["dev"]),
        pinned=pinned, revoked_op_certs=frozenset(state.revoked_op_certs),
    )  # fmt: skip


def current_offers(service: WorkspaceService, state: Any) -> dict[str, list[dict[str, Any]]]:
    """The offered items of the current binding, from the stored bundles verified again now. A bundle that does not
    verify
    (a hand edit, an expired card, a revoked op cert) offers nothing."""
    from orch.hubclient.errors import HubRefused

    doc = _offers_doc(service, state)
    held = _held(service, state)
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    if held is None:
        return out
    for kind in KINDS:
        if doc.get(kind) is None:
            continue
        try:
            out[kind] = list(_verify(service, state, kind, doc[kind], None, held)[_ITEMS[kind]])
        except (HubRefused, KeyError, TypeError):
            out[kind] = []
    return out


# -- accepted --------------------------------------------------------------------------------------------------------


def _entries(service: WorkspaceService) -> list[dict[str, Any]]:
    doc = _read(service, "accepted.json")
    rows = doc.get("entries") if type(doc) is dict else None
    if type(rows) is not list:
        return []
    keys = {"hub_id", "binding_seq", "kind", "name", "digest", "at", "by"}
    return [r for r in rows if type(r) is dict and set(r) == keys and r["kind"] in KINDS]


def accepted_entries(service: WorkspaceService, state: Any) -> list[dict[str, Any]]:
    """The accepts of the CURRENT binding."""
    b = _binding(state)
    return [r for r in _entries(service) if r["hub_id"] == b["hub_id"] and r["binding_seq"] == b["binding_seq"]]


def isolated(service: WorkspaceService) -> bool:
    """Do agents of this host run as their own OS user (tier B)? Read from the system each time (the detection is
    cheap and
    never raises); ``broken`` counts as not isolated, and so does every doubt."""
    try:
        from orch.runner.service import agent_tier

        return bool(agent_tier(Path(service.state_dir)).kind == "B")
    except Exception:  # noqa: BLE001 - any doubt is "not isolated"
        return False


def _status(item: dict[str, Any], kind: str, accepts: dict[str, dict[str, Any]], iso: Any) -> str:
    entry = accepts.get(item["name"])
    if entry is None:
        return "off"
    if entry["digest"] != item["digest"]:
        return "changed"
    if kind == "mcp" and item["auth"] == "hub_secret" and not iso():
        return "needs_isolation"  # the credential would sit where an agent can read it
    return "on"


def offers_view(service: WorkspaceService, *, detail: bool = False) -> dict[str, Any]:
    """Names, status and digests of every offered item. No text, no address, no description (an agent may read this)."""
    state = _state(service)
    offers = current_offers(service, state)
    accepts = {(r["kind"], r["name"]): r for r in accepted_entries(service, state)}
    cache: list[bool] = []

    def iso() -> bool:
        if not cache:
            cache.append(isolated(service))
        return cache[0]

    out: dict[str, Any] = {"hub_id": state.hub_id}
    for kind in KINDS:
        by_name = {n: r for (k, n), r in accepts.items() if k == kind}
        rows = []
        for item in offers[kind]:
            row = {"name": item["name"], "digest": item["digest"], "status": _status(item, kind, by_name, iso)}
            if kind == "mcp":
                row["auth"] = item["auth"]
            rows.append(row)
        out["skills" if kind == "skill" else "mcp"] = rows
    return out


def show(service: WorkspaceService, kind: str, name: str) -> dict[str, Any]:
    """Everything the owner needs to decide: the full text or the exact address, sign-in, secret name and
    description."""
    state = _state(service)
    item = next((i for i in current_offers(service, state)[kind] if i["name"] == name), None)
    if item is None:
        raise HostError("not_found", f"no offered {kind} called {name!r}: orch org sync")
    entry = next((r for r in accepted_entries(service, state) if r["kind"] == kind and r["name"] == name), None)
    iso = isolated(service) if kind == "mcp" and item["auth"] == "hub_secret" else True
    status = _status(item, kind, {name: entry} if entry else {}, lambda: iso)
    out = {"kind": kind, "status": status, "item": item, "notes": _NOTES[kind], "untrusted": True}
    if status == "needs_isolation" or (kind == "mcp" and item["auth"] == "hub_secret" and not iso):
        out["needs_isolation"] = "needs a separate agent user: this server's secret cannot be kept from an agent here"
    if kind == "mcp":
        out["tools"] = _tool_rows(service, state, name, item["digest"])
    return out


def accept(service: WorkspaceService, kind: str, name: str, digest: str, who: str) -> dict[str, Any]:
    with _MU:
        state = _state(service)
        item = next((i for i in current_offers(service, state)[kind] if i["name"] == name), None)
        if item is None:
            raise HostError("not_found", f"no offered {kind} called {name!r}: orch org sync")
        if item["digest"] != digest:
            raise HostError(
                "invalid.input", f"org: changed: {name!r} is not the version you were shown; look again: orch org show"
            )
        if kind == "mcp" and item["auth"] == "hub_secret" and not isolated(service):
            raise HostError(
                "invalid.input",
                f"org: needs_isolation: {name!r} needs a separate agent user; it stays Off on this computer",
            )
        b = _binding(state)
        rows = [
            r for r in _entries(service) if not (r["kind"] == kind and r["name"] == name and r["hub_id"] == b["hub_id"])
        ]
        if kind == "mcp" and not any(r["kind"] == kind and r["name"] == name for r in accepted_entries(service, state)):
            _drop_server(
                service, name
            )  # a fresh accept (never accepted, or withdrawn before) starts with every tool Off
        rows.append({**b, "kind": kind, "name": name, "digest": digest, "at": _now_ms(service), "by": who})
        _write(service, "accepted.json", {"entries": rows})
        return {"kind": kind, "name": name, "status": "on"}


def decline(service: WorkspaceService, kind: str, name: str) -> dict[str, Any]:
    with _MU:
        state = _state(service)
        b = _binding(state)
        rows = _entries(service)
        keep = [r for r in rows if not (r["kind"] == kind and r["name"] == name and r["hub_id"] == b["hub_id"])]
        if len(keep) != len(rows):
            _write(service, "accepted.json", {"entries": keep})
        if kind == "mcp":
            _drop_server(service, name)  # a re-accept starts with every tool Off
        return {"kind": kind, "name": name, "status": "off"}


# -- tools -----------------------------------------------------------------------------------------------------------


def _tool_entries(service: WorkspaceService) -> list[dict[str, Any]]:
    doc = _read(service, "tools.json")
    rows = doc.get("entries") if type(doc) is dict else None
    if type(rows) is not list:
        return []
    return [
        r for r in rows
        if type(r) is dict and set(r) == _TOOL_KEYS and type(r["tool"]) is str and type(r["pin"]) is str
        and type(r["server"]) is str and type(r["server_digest"]) is str
    ]  # fmt: skip


def _seen_servers(service: WorkspaceService) -> dict[str, Any]:
    doc = _read(service, "seen.json")
    servers = doc.get("servers") if type(doc) is dict else None
    return servers if type(servers) is dict else {}


def _drop_server(service: WorkspaceService, name: str) -> None:
    """Forget the tools the owner turned On for a server and what was seen of it (any binding: both are tied to it)."""
    with _MU:
        entries = _tool_entries(service)
        keep = [r for r in entries if r["server"] != name]
        if len(keep) != len(entries):
            _write(service, "tools.json", {"entries": keep})
        seen = _seen_servers(service)
        if name in seen:
            _write(service, "seen.json", {"servers": {k: v for k, v in seen.items() if k != name}})


def _seen_tools(service: WorkspaceService, state: Any, server: str, digest: str) -> list[dict[str, str]]:
    """The tools last seen for this server under the current binding and exactly this server digest."""
    rec = _seen_servers(service).get(server)
    if type(rec) is not dict or {k: rec.get(k) for k in ("hub_id", "binding_seq")} != _binding(state):
        return []
    if rec.get("digest") != digest or type(rec.get("tools")) is not list:
        return []
    return [t for t in rec["tools"] if type(t) is dict and {"name", "description", "pin"} <= set(t)]


def record_seen(service: WorkspaceService, server: str, server_digest: str, tools: list[dict[str, Any]]) -> None:
    """The proxy saw ``tools/list`` of a server: keep name, description (cut) and pin of each tool so the owner can
    look at them. Grants nothing. Wrong input (a bad server name, malformed tools) or no hub binding: no effect."""
    from .mcpshared import server_name_ok

    try:
        if not server_name_ok(server) or type(server_digest) is not str or type(tools) is not list:
            return
        rows: list[dict[str, str]] = []
        for t in tools[:MAX_SEEN_TOOLS]:
            if type(t) is not dict or type(t.get("name")) is not str or not _TOOL_NAME.fullmatch(t["name"]):
                return
            if type(t.get("pin")) is not str or not _PIN.fullmatch(t["pin"]):
                return
            desc = t.get("description")
            if desc is not None and type(desc) is not str:
                return
            rows.append({"name": t["name"], "description": (desc or "")[:MAX_DESCRIPTION], "pin": t["pin"]})
        with _MU:
            state = _state(service)
            seen = _seen_servers(service)
            b = _binding(state)
            old = seen.get(server)
            if (
                type(old) is dict
                and old.get("digest") == server_digest
                and old.get("tools") == rows
                and all(old.get(k) == v for k, v in b.items())
            ):
                return
            seen[server] = {**b, "digest": server_digest, "at": _now_ms(service), "tools": rows}
            _write(service, "seen.json", {"servers": seen})
    except Exception:  # noqa: BLE001 - a listing the owner never sees must never break a tool call
        return


def tool_policy(service: WorkspaceService, server: str) -> dict[str, str]:
    """``{tool: pin}`` the owner turned On, for the current binding and the server as it is served right now. A server
    that is not served, or whose digest changed, has no On tool. Never raises."""
    try:
        with _MU:
            state = _state(service)
            rec = next((r for r in served(service)["mcp"] if r["name"] == server), None)
            if rec is None:
                return {}
            b = _binding(state)
            return {
                r["tool"]: r["pin"]
                for r in _tool_entries(service)
                if r["hub_id"] == b["hub_id"] and r["binding_seq"] == b["binding_seq"] and r["server"] == server
                and r["server_digest"] == rec["digest"] and _PIN.fullmatch(r["pin"])
            }  # fmt: skip
    except Exception:  # noqa: BLE001 - any doubt is "nothing is On"
        return {}


def _tool_rows(service: WorkspaceService, state: Any, server: str, digest: str) -> list[dict[str, str]]:
    on = tool_policy(service, server)
    b = _binding(state)
    mine = {
        r["tool"] for r in _tool_entries(service)
        if r["hub_id"] == b["hub_id"] and r["binding_seq"] == b["binding_seq"] and r["server"] == server
    }  # fmt: skip
    out = []
    for t in _seen_tools(service, state, server, digest):
        if on.get(t["name"]) == t["pin"]:
            status = "on"
        elif t["name"] in mine:
            status = "changed"  # turned On once for something else: the owner must click again
        else:
            status = "off"
        out.append({"name": t["name"], "description": t["description"], "pin": t["pin"], "status": status})
    return out


def set_tool(service: WorkspaceService, server: str, tool: str, on: bool, pin: str | None, who: str) -> dict[str, Any]:
    """Turn one tool of an accepted server On (for exactly the pin shown) or Off (always works)."""
    with _MU:
        state = _state(service)
        rec = next((r for r in served(service)["mcp"] if r["name"] == server), None)
        if rec is None:
            raise HostError("not_found", f"company tool server {server!r} is not on: orch org accept mcp {server}")
        b = _binding(state)
        rows = [
            r for r in _tool_entries(service) if r["hub_id"] == b["hub_id"] and r["binding_seq"] == b["binding_seq"]
        ]
        keep = [r for r in rows if not (r["server"] == server and r["tool"] == tool)]
        if on:
            seen = next((t for t in _seen_tools(service, state, server, rec["digest"]) if t["name"] == tool), None)
            if seen is None:
                raise HostError("not_found", f"no tool {tool!r} seen on {server!r} yet: let an agent list its tools")
            if pin is None or pin != seen["pin"]:
                raise HostError(
                    "invalid.input",
                    f"org: changed: {tool!r} is not the version you were shown; look again: orch org show",
                )
            keep.append({**b, "server": server, "server_digest": rec["digest"], "tool": tool, "pin": pin,
                         "at": _now_ms(service), "by": who})  # fmt: skip
        if keep != _tool_entries(service) or on:
            _write(service, "tools.json", {"entries": keep})
        return {"server": server, "tool": tool, "status": "on" if on else "off"}


def served(service: WorkspaceService) -> dict[str, list[dict[str, Any]]]:
    """What a launch may use: accepted, the same digest in the verified offer, the current binding. Empty when the
    workspace is not bound to a hub (never raises for that: no hub, no company content)."""
    try:
        state = _state(service)
    except HostError:
        return {"skills": [], "mcp": []}
    offers = current_offers(service, state)
    accepts = {(r["kind"], r["name"]): r["digest"] for r in accepted_entries(service, state)}
    out: dict[str, list[dict[str, Any]]] = {"skills": [], "mcp": []}
    for kind, key in (("skill", "skills"), ("mcp", "mcp")):
        out[key] = [i for i in offers[kind] if accepts.get((kind, i["name"])) == i["digest"]]
    if out["mcp"] and not isolated(service):
        out["mcp"] = [i for i in out["mcp"] if i["auth"] != "hub_secret"]  # tier A: no credential servers (condition 3)
    return out


# -- sync ------------------------------------------------------------------------------------------------------------


def sync_bundles(service: WorkspaceService, bodies: dict[str, Any]) -> dict[str, str]:
    """Take the two reply bodies of ``org_skills_get`` / ``mcp_servers_get`` (an error string for one the hub refused),
    verify each against the card and the held bundle (no rollback), store what is new and prune the accepts of
    withdrawn items. Returns ``{kind: "new" | "same" | <refusal code>}``. Nothing is enabled by this."""
    from orch.hubclient.errors import HubRefused

    result: dict[str, str] = {}
    with _MU:
        state = _state(service)
        held = _held(service, state)
        if held is None:
            raise HostError("hub.refused", "the hub card could not be verified now: nothing was changed")
        doc = _offers_doc(service, state)
        for kind in KINDS:
            body = bodies.get(kind)
            if type(body) is str:
                result[kind] = body
                continue
            try:
                signed = body["bundle"]
                have = doc.get(kind)
                pinned = None
                if have is not None:
                    try:
                        pinned = _verify(service, state, kind, have, None, held)
                    except HubRefused:
                        pinned = None  # the stored one no longer verifies: it pins nothing
                new = _verify(service, state, kind, signed, pinned, held)
            except HubRefused as exc:
                result[kind] = exc.code
                continue
            except (KeyError, TypeError):
                result[kind] = "malformed"
                continue
            if pinned is not None and new["version"] == pinned["version"]:
                result[kind] = "same"
                continue
            doc[kind] = signed
            result[kind] = "new"
        _write(service, "offers.json", doc)
        # prune: an accept whose item is no longer offered is gone (a re-offer needs a new accept), and so is one of
        # another
        # binding. Only for a kind this sync verified fresh: a refusal or an unreachable hub must never wipe an accept.
        offers = current_offers(service, state)
        fresh = {k for k in KINDS if result.get(k) in ("new", "same")}
        names = {(k, i["name"]) for k in KINDS for i in offers[k]}
        b = _binding(state)
        rows = _entries(service)

        def keeps(r: dict[str, Any]) -> bool:
            if r["hub_id"] != b["hub_id"] or r["binding_seq"] != b["binding_seq"]:
                return False
            return r["kind"] not in fresh or (r["kind"], r["name"]) in names

        keep = [r for r in rows if keeps(r)]
        if len(keep) != len(rows):
            _write(service, "accepted.json", {"entries": keep})
    return result
