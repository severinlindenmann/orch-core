"""Hub settings routes (M4, #1460 read, #1461 write). A mixin of :class:`~orch_hub.webapi.api.WebApi` (it only uses
``self.d``) plus its route table. The read answer is built from ``hub-settings.json`` and constants; it carries no key,
token or password. The writes are admin only (the pipeline's cookie, CSRF, origin and switch-ticket rules apply), change
the file atomically and are audited BEFORE they are saved (condition 23: a chain that cannot be written refuses the
change). Nothing signed changes: advertised addresses are hints under the TLS pin. The address the phone pairs with is
the first entry and stays as it is here (changing it rotates the hub's keys: its own, later step)."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import colocation as CO
from ..contract.phase4 import render_text, strip_text, text_ok
from ..settings import ADDRESSES_MAX, KINDS, NAME_MAX, SettingsError, load, origin_of, save

#: how people sign in today (read only: password + code is the one method, so it cannot be turned off; later methods are not listed)
SIGN_IN = ({"id": "password", "label": "Name + password + 6-digit code", "note": "For everyone", "on": True},)


_NET_100 = ipaddress.ip_network("100.64.0.0/10")  # the company's private network (VPN) range
_PORT = re.compile(r":([0-9]{1,5})$")
_HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,98}[A-Za-z0-9])?")


def _inet_lines() -> str:
    for argv in (["ip", "-o", "-4", "addr"], ["ifconfig"]):
        try:
            out = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=5).stdout
        except OSError, subprocess.SubprocessError:
            out = ""
        if out:
            return out
    return ""


def address_kind(ip: str) -> str:
    """The settings kind of an IP address: private (100.64/10), server (loopback), office (any other private), else public."""
    a = ipaddress.ip_address(ip)
    if a.is_loopback:
        return "server"
    if a in _NET_100:
        return "private"
    return "office" if a.is_private else "public"


_VIRTUAL = re.compile(
    r"^(docker|br-|virbr|veth|cni|flannel|cali|lxc|lxd|bridge|vmnet|vboxnet|utun|zt)"
)  # bridges, container links and tunnels: never a good first pick
_IP_O = re.compile(r"^\d+:\s+(\S+?)(?:@\S+)?\s+inet (\d+\.\d+\.\d+\.\d+)")
_IFC_HEAD = re.compile(r"^([A-Za-z0-9_.@-]+?):\s+flags")


def detect_addresses(text: str | None = None) -> list[dict[str, str]]:
    """This server's IPv4 addresses with their kind (loopback first, then real interfaces, virtual bridges last) and, when
    the output names it, the interface. ``text`` is ``ip -o -4 addr`` or ``ifconfig`` output."""
    found: list[dict[str, str]] = [{"ip": "127.0.0.1", "kind": "server"}]
    iface = ""
    for line in (_inet_lines() if text is None else text).splitlines():
        head = _IFC_HEAD.match(line)
        if head:
            iface = head.group(1)
        flat = _IP_O.match(line)
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if m is None:
            continue
        ip = m.group(1)
        name = flat.group(1) if flat else iface
        try:
            ok = not ipaddress.ip_address(ip).is_loopback and not ip.startswith("169.254.")
        except ValueError:
            ok = False
        if ok and all(f["ip"] != ip for f in found):
            found.append({"ip": ip, "kind": address_kind(ip), **({"iface": name} if name else {})})
    rest = sorted(found[1:], key=lambda f: bool(_VIRTUAL.match(f.get("iface", ""))))  # stable: real interfaces first
    return [found[0], *rest][:16]


@dataclass
class SettingsControl:
    """What the settings routes need from the running process. ``bind_all``: the hub listens on every interface, so it can
    advertise another address (a loopback-only hub cannot). ``restart``: schedules the self-restart (None = a hub
    started by hand: the file is saved and the answer says it was not restarted). ``assigned``: is this IP one of this
    computer's? ``detect``: this computer's addresses (the picker)."""

    bind_all: bool = False
    restart: Callable[[], None] | None = None
    assigned: Callable[[str], bool] = CO.locally_assigned
    detect: Callable[[], list[dict[str, str]]] = field(default=detect_addresses)
    #: the start-up origin rules of this process (``HubConfig``): called with the new console origins and the API origin
    #: BEFORE anything is audited or saved, and raises ValueError for a set the next start would refuse (e.g. http
    #: origins behind the edge), so a change can never leave a hub that crash-loops
    check_origins: Callable[[frozenset[str], str], None] | None = None


def settings_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "GET",
            "/admin/settings",
            "admin",
            "a_settings",
            "Hub settings: name, company, the advertised addresses, sign-in methods",
        ),
        r(
            "PUT",
            "/admin/settings/general",
            "admin",
            "a_settings_general",
            "Change the hub's name and company (audited)",
        ),
        r(
            "GET",
            "/admin/settings/addresses/detected",
            "admin",
            "a_settings_detected",
            "This server's own addresses, for the address picker",
        ),
        r(
            "PUT",
            "/admin/settings/addresses",
            "admin",
            "a_settings_addresses",
            "Change the addresses the hub advertises (audited; the hub restarts to use them)",
        ),
    )


_LOCK = threading.Lock()  # one settings write at a time (read, audit, save, restart request)


def _err(code: str) -> Exception:
    from .api import ApiError  # late: api.py imports this module

    return ApiError(code)


def _exact(b: dict | None, required: set[str], optional: frozenset[str] = frozenset()) -> dict:
    if b is None or not required <= set(b) <= required | optional:
        raise _err("malformed")
    return b


def _text(x: Any, *, empty_ok: bool) -> str:
    """Plain text from a form: control, format (bidi) and separator characters are removed, outer spaces trimmed."""
    if type(x) is not str or len(x) > 4 * NAME_MAX:
        raise _err("malformed")
    t = strip_text(x).strip()
    if t == "" and empty_ok:
        return t
    if len(t) > NAME_MAX or not text_ok(t, NAME_MAX):
        raise _err("malformed")
    return t


def _host_part(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _host_of(web: str) -> str:
    return web.split("://", 1)[1].rsplit(":", 1)[0].strip("[]")


def _is_installed(a: Any, entry: dict[str, Any]) -> bool:
    """The request names the entry the install made (same kind, host and scheme, nothing else)."""
    return (
        type(a) is dict
        and set(a) <= {"kind", "host", "scheme"}
        and a.get("kind") == entry["kind"]
        and a.get("host") == _host_of(entry["web"])
        and a.get("scheme", "http") == entry["web"].split(":", 1)[0]
    )


def _port(addr: str) -> int:
    m = _PORT.search(addr)
    assert m is not None  # the settings validator guarantees host:port
    return int(m.group(1))


def _entry(a: Any, web_port: int, link_port: int, ctl: SettingsControl) -> dict[str, Any]:
    """One requested address as a settings entry. A private, office or server address must be one of THIS computer's own
    addresses of that kind (an admin cannot make the hub advertise, and allow as an origin, a stranger's address); a
    public one is never made here (the public address belongs to the edge setup): only an unchanged one passes."""
    a = _exact(a, {"kind", "host"}, frozenset({"scheme"}))
    kind, host, scheme = a["kind"], a["host"], a.get("scheme", "http")
    if kind not in KINDS or kind == "public" or type(host) is not str or scheme != "http" or len(host) > 100:
        raise _err("malformed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:  # a name, a number such as 2130706433 or 0x7f.1: only addresses of this computer, as dotted IPs
        raise _err("malformed")
    host = str(ip)
    if ip.version != 4 or ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local:
        raise _err("malformed")
    if address_kind(host) != kind or not ctl.assigned(host):  # the kind fits and it is one of THIS computer's own
        raise _err("malformed")
    h = _host_part(host)
    return {
        "kind": kind,
        "web": f"{scheme}://{h}:{web_port}",
        "link": f"{h}:{link_port}",
    }


class SettingsHandlers:
    d: Any

    def _h_a_settings(self, c: Any, p: dict, q: dict, b: dict | None):
        file = self.d.settings_file
        ctl = self.d.settings_ctl
        try:
            s = load(file.parent) if file is not None else None
        except SettingsError:
            return 200, {"configured": False, "problem": "The settings file cannot be read."}
        if s is None:
            return 200, {"configured": False, "problem": None}
        return 200, {
            "configured": True,
            "name": render_text(s["name"]),
            "company": render_text(s["company"]),
            "addresses": [{"kind": a["kind"], "web": a["web"]} for a in s["addresses"]],
            "phone_kind": s["addresses"][0]["kind"],  # the address phones pair with: not changed here
            "can_change_addresses": bool(ctl is not None and ctl.bind_all),
            "restarts": bool(ctl is not None and ctl.restart is not None),
            "sign_in": list(SIGN_IN),
        }

    # --- writes (#1461) --------------------------------------------------------------------------------------------------

    def _settings_now(self) -> tuple[Any, dict[str, Any]]:
        file = self.d.settings_file
        if file is None:
            raise _err("unavailable")
        try:
            cur = load(file.parent)
        except SettingsError:
            raise _err("unavailable") from None
        if cur is None:
            raise _err("unavailable")
        return file.parent, cur

    def _settings_commit(self, c: Any, state: Any, new: dict[str, Any], change: str) -> None:
        """Audit first (a chain that cannot be written refuses the change), then save, then say it in the activity."""
        self.d.audit.hub_settings(change=change, actor_user_id=c.user_id)
        try:
            save(state, new)
        except SettingsError:
            raise _err("malformed") from None
        if self.d.activity is not None:
            self.d.activity.record("hub.settings.changed", actor_id=c.user_id, detail={"setting": change})

    def _h_a_settings_general(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _exact(b, {"name", "company"})
        name, company = _text(b["name"], empty_ok=False), _text(b["company"], empty_ok=True)
        with _LOCK:
            state, cur = self._settings_now()
            if (cur["name"], cur["company"]) != (name, company):
                self._settings_commit(c, state, {**cur, "name": name, "company": company}, "general")
                if self.d.switch is not None:
                    self.d.switch.hub_name = name  # the rail shows it at once
        return 200, {"ok": True, "name": render_text(name), "company": render_text(company)}

    def _ctl(self) -> SettingsControl:
        if self.d.settings_ctl is None:
            raise _err("unavailable")
        return self.d.settings_ctl

    def _h_a_settings_detected(self, c: Any, p: dict, q: dict, b: dict | None):
        ctl = self._ctl()
        return 200, {"addresses": ctl.detect(), "can_change": ctl.bind_all}

    def _h_a_settings_addresses(self, c: Any, p: dict, q: dict, b: dict | None):
        ctl = self._ctl()
        if not ctl.bind_all:  # re-binding is the installer's job: a loopback-only hub cannot advertise another address
            raise _err("unavailable")
        b = _exact(b, {"addresses"})
        asked = b["addresses"]
        if type(asked) is not list or not 1 <= len(asked) <= ADDRESSES_MAX:
            raise _err("malformed")
        with _LOCK:
            state, cur = self._settings_now()
            primary = cur["addresses"][0]
            links = [a["link"] for a in cur["addresses"] if a["link"] is not None]
            if not links:
                raise _err("unavailable")
            web_port, link_port = _port(primary["web"]), _port(links[0])
            pub = next((a for a in cur["addresses"] if a["kind"] == "public"), None)
            entries = []
            for a in asked:
                if type(a) is dict and a.get("kind") == "public":  # only the install's own public entry, as it is
                    if pub is None or set(a) - {"kind", "host", "scheme"} or a.get("host") != _host_of(pub["web"]):
                        raise _err("malformed")
                    if a.get("scheme", "http") != pub["web"].split(":", 1)[0]:
                        raise _err("malformed")
                    entries.append(pub)
                elif _is_installed(a, primary):  # the phone address is kept verbatim, never re-validated
                    entries.append(primary)
                else:
                    entries.append(_entry(a, web_port, link_port, ctl))
            if pub is not None and pub not in entries:
                entries.append(pub)  # not touched by this route
            if len({e["kind"] for e in entries}) != len(entries) or len({e["web"] for e in entries}) != len(entries):
                raise _err("malformed")
            if len(entries) > ADDRESSES_MAX:
                raise _err("malformed")
            if primary not in entries:  # the phone address is the first entry and is not changed by this route
                raise _err("phone_address_locked")
            entries.remove(primary)
            entries.insert(0, primary)
            if entries == cur["addresses"]:
                return 200, {"ok": True, "changed": False, "restarting": False}
            if ctl.check_origins is not None:
                try:
                    ctl.check_origins(frozenset(origin_of(e["web"]) for e in entries), origin_of(entries[0]["web"]))
                except ValueError:
                    raise _err(
                        "malformed"
                    ) from None  # the next start would refuse these origins: nothing is audited or saved
            self._settings_commit(c, state, {**cur, "addresses": entries}, "addresses")
            if ctl.restart is None:
                return 200, {"ok": True, "changed": True, "restarting": False}
            ctl.restart()
        return 202, {"ok": True, "changed": True, "restarting": True}
