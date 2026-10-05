"""The Remote tab (Workspace & addons): pair, approve, scope, revoke and cut off remote devices.

Every POST here is a human-only dashboard action on this computer, and the whole family sits under /workspace, which
the remote gate refuses for every device: a device can never pair, rescope or revoke anything, itself included. Each
POST also needs the same-origin check and refuses a process with an agent harness in its ancestry, like the CLI's
human verbs. A device is added to the registry only here (`Host.approve`); nothing else calls it.

The tab works on what the bridge loop puts on the app: `app.state.bridge_host` (the library's Host, or None) and
`app.state.bridge_link` (orch.remote.bridge_link.BridgeLink). Without a host it shows the registry read-only when
`app.state.bridge_registry` (a Registry) is set, and changes nothing. Everything a device supplied (label, subject)
is shown as escaped text, the label reduced to letters, digits, space, dot, underscore and hyphen (40 characters).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from filelock import Timeout

from orch import actor as actor_mod
from orch.dashboard.auth import strict_same_origin
from orch.dashboard.reach import remote_origin
from orch.dashboard.views import back, confirm_page
from orch.errors import OrchError
from orch.remote import store as phone_store
from orch.remote.bridge_host import files
from orch.remote.bridge_link import NullLink

router = APIRouter()

_REMOTE = "/workspace?tab=remote"
SCOPE_NAMES = ("look", "decide", "operate", "type")
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]")
_CODE = re.compile(r"[a-z0-9_]{1,40}")
_FAILS = (files.Damaged, LookupError, ValueError, OSError, Timeout)
NOT_RUNNING = "Remote is not running: start the dashboard with --remote"

# What a fixed link error code means, and what to do.
LINK_ERRORS = {
    "host_taken": "Another dashboard already holds this workspace's remote connection. Close the other one, then start"
                  " this dashboard again with --remote.",
    "unauthorized": "The relay did not accept this computer's sign-in. Sign in again, then start"
                    " the dashboard with --remote.",
    "key_missing": "The workspace key is not available on this computer. Set the workspace up for Remote again.",
    "network": "The relay cannot be reached. Check this computer's internet connection; the connection retries by itself.",
}
LINK_STATES = {"off": "Not connected", "connecting": "Connecting", "online": "Online", "reconnecting": "Reconnecting",
               "stopped": "Disconnected by you", "error": "Not connected: error"}
WARNINGS = {
    "operate": "Editing tickets can steer running agents; agents read ticket text.",
    "type": "Type lets this device run things on this Mac: it can type into terminals, start agents and arm the AI"
            " Factory. Give it only to a device you hold in your hand.",
}


def safe_text(value, limit: int = 300) -> str:
    """Host- or device-supplied text for display: escaped by the template, and reduced here to printable characters."""
    return "".join(c for c in str(value) if c.isprintable())[:limit]


def safe_label(value) -> str:
    return _UNSAFE.sub("", str(value))[:40].strip()


def _when(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _host(request: Request):
    return getattr(request.app.state, "bridge_host", None)


def _link(request: Request):
    return getattr(request.app.state, "bridge_link", None) or NullLink()


def _registry(request: Request):
    host = _host(request)
    return host.registry if host is not None else getattr(request.app.state, "bridge_registry", None)


def _gate(request: Request) -> PlainTextResponse | None:
    """None when this POST may go on: same origin, not from a remote device, no agent harness behind this process."""
    if not strict_same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if remote_origin(request) is not None or actor_mod.agent_harness():
        return PlainTextResponse("refused: this is a human-only action on this computer", status_code=403)
    return None


# -- what the tab shows ------------------------------------------------------------------------------------------

def _link_view(request: Request) -> dict:
    try:
        st = _link(request).status()
        state, err = str(st.get("state", "off")), st.get("last_error")
    except Exception:  # noqa: BLE001 - a broken link object never breaks the page
        state, err = "error", None
    code = err if isinstance(err, str) and _CODE.fullmatch(err) else None
    sentence = None
    if state == "error" or code:
        sentence = LINK_ERRORS.get(code) or "The connection stopped with an error." + (f" (code: {code})" if code else "")
    return {"state": state, "label": LINK_STATES.get(state, "Unknown"), "sentence": sentence,
            "ok": state == "online", "bad": state == "error", "can_disconnect": state in ("connecting", "online", "reconnecting")}


def context(request: Request) -> dict:
    """Everything the Remote tab renders; never raises."""
    from orch.remote.bridge_host.keys import device_fingerprint
    host, registry = _host(request), _registry(request)
    out = {"running": host is not None, "link": _link_view(request), "devices": [], "pending": [], "activity": [],
           "damaged": None, "readable": registry is not None, "scopes": SCOPE_NAMES, "warnings": WARNINGS}
    if host is not None:
        h = host.health()
        if not h.get("registry_readable", True):
            out["damaged"] = "The device registry cannot be read. Remote answers nothing until it is repaired, and no change can be made."
        elif h.get("damaged_records"):
            out["damaged"] = f"{len(h['damaged_records'])} stored request record(s) are damaged and are kept for you to look at. Requests that cannot be recorded are not run."
    if registry is None:
        return out
    try:
        devices = registry.devices()
        entries = registry.audit_entries(60)
    except files.Damaged:
        out["damaged"] = out["damaged"] or "The device registry cannot be read. Remote answers nothing until it is repaired, and no change can be made."
        return out
    names = {d.id: safe_label(d.label) or d.id[:8] for d in devices.values()}
    for d in sorted(devices.values(), key=lambda d: (d.revoked, -d.paired_at)):
        seen = host.store.last_seen_ms(d.id) if host is not None else None
        out["devices"].append({
            "id": d.id, "label": names[d.id], "kind": "Phone" if d.phone_link else "Browser or computer",
            "scope": d.scope, "revoked": d.revoked, "fingerprint": device_fingerprint(d.pub),
            "last_seen": _when(seen) if seen else "never", "paired": _when(d.paired_at),
            "linked": bool(d.phone_link), "credential": None if d.credential is None else (
                "synced passkey" if d.credential.be else "passkey on this device"),
        })
    if host is not None:
        for did, p in list(host.pairing.pending.items()):
            if p.state == "pending":
                out["pending"].append({
                    "id": did, "label": safe_label(p.label) or "(no name)", "scope": p.scope,
                    "scopes": SCOPE_NAMES[:SCOPE_NAMES.index(p.scope) + 1], "fingerprint": p.fingerprint,
                    "linked": bool(p.phone_link), "credential": None if p.credential is None else (
                        "synced passkey" if p.credential.be else "passkey on the device")})
    out["activity"] = [_entry(e, names) for e in entries if e["event"] != "sign_count"]
    return out


def _entry(e: dict, names: dict) -> dict:
    who = names.get(e.get("device")) or safe_text(str(e.get("device", ""))[:8], 8)
    ev = e["event"]
    detail = ""
    if ev == "added":
        text, detail = f"Device added: {safe_label(e.get('label', ''))}", f"scope {safe_text(e.get('scope'), 10)}"
    elif ev == "scope_changed":
        text, detail = f"Scope changed: {who}", f"now {safe_text(e.get('scope'), 10)}"
    elif ev == "revoked":
        text = f"Revoked: {who}"
    elif ev == "pairing_rejected":
        text = f"Pairing rejected: {who}"
    elif ev == "assertion":
        sub = e.get("subject") if isinstance(e.get("subject"), dict) else {}
        ok = e.get("ok") is True
        text = f"Confirmed on {who}: {safe_text(sub.get('kind', ''), 40)}" if ok else f"Confirmation refused on {who}"
        detail = safe_text(sub.get("shown", ""), 300) if ok else safe_text(e.get("why") or "", 40)
    else:
        text = safe_text(ev, 40)
    return {"at": _when(e["at"]), "text": text, "detail": detail}


# -- the POSTs ---------------------------------------------------------------------------------------------------

def _need_host(request: Request):
    host = _host(request)
    return host, (None if host is not None else back(_REMOTE, err=NOT_RUNNING))


def _scope_form(scope: str, allow_type: str) -> str | None:
    """The chosen scope, or None when it is unknown or Type was chosen without the explicit Type switch."""
    return scope if scope in SCOPE_NAMES and (scope != "type" or allow_type == "1") else None


@router.post("/workspace/remote/offer")
def remote_offer(request: Request, scope: str = Form("look"), allow_type: str = Form("")):
    if (r := _gate(request)) is not None:
        return r
    host, none = _need_host(request)
    if none is not None:
        return none
    if (scope := _scope_form(scope, allow_type)) is None:
        return back(_REMOTE, err="choose a scope; Type also needs its own switch")
    try:
        offer, fragment = host.offer(scope)
    except _FAILS as e:
        return back(_REMOTE, err=f"could not create the offer: {safe_text(e, 100)}")
    url = f"{str(host.origin).rstrip('/')}/remote/pair#{fragment}"
    token = request.app.state.reveals.put({"kind": "remote_offer", "url": url, "scope": scope,
                                           "expires": _when(offer.expires_ms)})
    return RedirectResponse(f"/workspace?tab=remote&offer={token}#remote", status_code=303)


@router.post("/workspace/remote/pending/{did}/reject")
def remote_reject(request: Request, did: str):
    if (r := _gate(request)) is not None:
        return r
    host, none = _need_host(request)
    if none is not None:
        return none
    try:
        host.reject(did)
    except _FAILS:
        return back(_REMOTE, err="no pending pairing for that device")
    return back(_REMOTE, msg="Pairing rejected")


def _last_group(fp: str) -> str:
    return fp.rsplit("-", 1)[-1]


@router.post("/workspace/remote/pending/{did}/approve")
def remote_approve(request: Request, did: str, scope: str = Form(""), last_group: str = Form(""),
                   allow_type: str = Form("")):
    """Approve only after the owner typed the last group of the fingerprint the device shows; the scope can only go
    down from what the offer asked for (the library refuses anything higher)."""
    if (r := _gate(request)) is not None:
        return r
    host, none = _need_host(request)
    if none is not None:
        return none
    p = host.pairing.pending.get(did)
    if p is None or p.state != "pending":
        return back(_REMOTE, err="no pending pairing for that device")
    if last_group.strip().upper() != _last_group(p.fingerprint):
        return back(_REMOTE, err="that is not the last group of the fingerprint: compare it on the device, or reject")
    if (scope := _scope_form(scope or p.scope, allow_type)) is None:  # Type is ticked at approval, never implied
        return back(_REMOTE, err="choose a scope; Type also needs its own switch")
    try:
        dev = host.approve(did, scope)
    except _FAILS as e:
        return back(_REMOTE, err=f"not approved: {safe_text(e, 100)}")
    return back(_REMOTE, msg=f"{safe_label(dev.label) or 'Device'} added with scope {dev.scope}")


@router.post("/workspace/remote/devices/{did}/scope")
def remote_scope(request: Request, did: str, scope: str = Form(...), allow_type: str = Form("")):
    if (r := _gate(request)) is not None:
        return r
    host, none = _need_host(request)
    if none is not None:
        return none
    try:
        current = host.registry.get(did)
    except files.Damaged:
        return back(_REMOTE, err="the device registry cannot be read")
    if current is None or current.revoked:
        return back(_REMOTE, err="no such active device")
    # Type needs its switch unless the device already has it (then the switch is simply still on)
    if (scope := _scope_form(scope, "1" if allow_type == "1" or current.scope == "type" else "")) is None:
        return back(_REMOTE, err="choose a scope; Type also needs its own switch")
    try:
        _close_streams(request, host.set_scope(did, scope), "scope_changed")
    except _FAILS as e:
        return back(_REMOTE, err=f"scope not changed: {safe_text(e, 100)}")
    return back(_REMOTE, msg=f"Scope is now {scope}")


def _close_streams(request: Request, rids, code: str) -> None:
    """End the device's open streams at once with `code` (the bridge loop's close_streams); without a loop, none run."""
    loop = getattr(request.app.state, "bridge_loop", None)
    if loop is not None and rids:
        loop.close_streams(rids, code)


def revoke_steps(registry, host, dev, ws_root, request=None) -> tuple[list[str], list[str]]:
    """Revoke `dev` here, in every other workspace's registry on this computer (matched by key, D7) and in the
    phone pairing linked to it. Safe to run again on an already revoked device: it then does the remaining steps.
    Returns (what was done, what failed); only a failure of the first step (revoking here) stops the others."""
    from orch.remote.bridge_host.registry import revoke_everywhere
    done: list[str] = []
    problems: list[str] = []
    now = host.clock() if host is not None else int(time.time() * 1000)
    if not dev.revoked:
        try:
            if host is not None:
                ended = host.revoke(dev.id)
                if request is not None:  # its open streams end now with `revoked`, not at their next frame
                    _close_streams(request, ended, "revoked")
            else:
                registry.revoke(dev.id, now)
            done.append("revoked here")
        except _FAILS as e:
            return done, [f"could not revoke it here: {safe_text(e, 100)}"]
    try:
        hits, damaged = revoke_everywhere(registry.root.parents[2], dev.pub, now)  # <config>/permits/bridge/<ws>
        done.append(f"revoked in {len(hits)} other workspace(s)")
        problems += [f"the registry of workspace {safe_text(w, 8)} cannot be read, so the device may still be live there"
                     for w in damaged]
    except _FAILS as e:
        problems.append(f"could not revoke it in the other workspaces: {safe_text(e, 100)}")
    if dev.phone_link:
        try:
            phone_store.revoke(ws_root, dev.phone_link)
            done.append("linked phone pairing revoked")
        except _FAILS + (OrchError,) as e:
            problems.append(f"could not revoke the linked phone pairing: {safe_text(e, 100)}")
    return done, problems


def revoke_linked_devices(request: Request, phone_id: str) -> tuple[int, list[str], list[str]]:
    """Revoking a phone pairing revokes the devices linked to it (either side revokes both). Called by the Phones
    tab's revoke; returns (how many devices, what failed, neutral notes) and never raises. Without a running host it works on the
    registry file when one is known, and says so when it cannot look."""
    host, registry = _host(request), _registry(request)
    if registry is None:
        # A neutral note, and only where Remote has been used on this computer (a bridge directory exists): this
        # workspace's own id is not known here. ponytail: per-workspace check once the loop provides the id.
        from orch.dashboard import launch
        used = (launch.config_dir() / "permits" / "bridge").is_dir()
        return 0, [], (["No remote devices could be checked: Remote is not running."] if used else [])
    try:
        linked = [d for d in registry.devices().values() if d.phone_link == phone_id]
    except files.Damaged:
        return 0, ["linked remote devices could not be revoked: the device registry cannot be read"], []
    n, problems = 0, []
    for d in linked:
        if d.revoked:
            continue  # its other steps are finished with "Finish revoking" on the Remote tab
        done, bad = revoke_steps(registry, host, d, request.app.state.ws.root, request)
        n += bool(done)
        problems += [f"{safe_label(d.label) or 'a device'}: {m}" for m in bad]
    return n, problems, []


@router.post("/workspace/remote/devices/{did}/revoke")
def remote_revoke(request: Request, did: str, ask: str = Form("")):
    """Also finishes a half-done revoke: on an already revoked device it runs the remaining steps again."""
    if (r := _gate(request)) is not None:
        return r
    host, none = _need_host(request)
    if none is not None:
        return none
    try:
        dev = host.registry.get(did)
    except files.Damaged:
        return back(_REMOTE, err="the device registry cannot be read")
    if dev is None:
        return back(_REMOTE, err="no such device")
    if ask:
        label = safe_label(dev.label) or "this device"
        extra = " Its linked phone pairing is revoked with it." if dev.phone_link else ""
        return confirm_page(request, action=f"/workspace/remote/devices/{did}/revoke", fields=[],
                            title=f"{'Finish revoking' if dev.revoked else 'Revoke'} {label}?",
                            body="It is refused from its next request, its open streams and waiting actions end, and it"
                                 " is revoked in every workspace on this computer. To use it again it must pair again."
                                 + extra, confirm="Finish revoking" if dev.revoked else "Revoke device",
                            cancel="Keep device", cancel_href=_REMOTE, danger=True, nav="workspace")
    done, problems = revoke_steps(host.registry, host, dev, request.app.state.ws.root, request)
    if problems:
        return back(_REMOTE, err="not fully revoked: " + "; ".join(problems) + ". Use Finish revoking to try again.",
                    msg=("Done: " + ", ".join(done)) if done else None)
    return back(_REMOTE, msg="Device revoked: " + ", ".join(done))


@router.post("/workspace/remote/disconnect")
def remote_disconnect(request: Request, ask: str = Form("")):
    if (r := _gate(request)) is not None:
        return r
    if ask:
        return confirm_page(request, action="/workspace/remote/disconnect", fields=[],
                            title="Disconnect remote now?",
                            body="Every remote request is refused and open remote streams end. This dashboard keeps"
                                 " running here. To connect again, start the dashboard again with --remote.",
                            confirm="Disconnect remote", cancel="Stay connected", cancel_href=_REMOTE, danger=True,
                            nav="workspace")
    if _host(request) is not None:
        _host(request).stop()
    _link(request).disconnect()
    return back(_REMOTE, msg="Remote disconnected")
