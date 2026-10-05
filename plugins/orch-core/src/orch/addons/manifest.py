"""The addon manifest `orch-addon.json` (spec A1 §5.1). Reading it never imports addon code."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from orch.errors import ValidationError

ADDON_API = "2"
MANIFEST_NAME = "orch-addon.json"
NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
RESERVED_NAMES = frozenset({"changed", "addons", "core", "orch"})  # state_dir/addons/changed, routes, core names
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
_ENTRY = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
_BINARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]*")
_KEY = re.compile(r"[a-z][a-z0-9_]*")
_TYPE = re.compile(r"\.[a-z0-9]{1,10}|[a-z]+/[a-z0-9.+-]+")  # an upload type: ".pdf" or "image/png"
MAX_UPLOAD_BYTES = 209_715_200  # 200 MiB, the hard cap for any accepts_file action
CAPABILITIES = frozenset({"provider", "page", "panel", "decisions", "settings", "events"})
SLOT_NAMES = frozenset({"today.summary", "today.from_addons", "ticket.code", "ticket.sync", "ticket.external",
                        "ticket.pages", "board.external", "workspace.settings", "guide.section"})
FIELD_TYPES = frozenset({"text", "select", "bool", "map"})
MENU_ICONS = {
    "box": "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5",
    "code": "M8 7l-5 5 5 5M16 7l5 5-5 5",
    "issues": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM12 8v5M12 16h.01",
    "status": "M4 12h4l2-5 4 10 2-5h4",
    "database": "M4 6c0-2 16-2 16 0v12c0 2-16 2-16 0zM4 6c0 2 16 2 16 0M4 12c0 2 16 2 16 0",
    "book": "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h11",
    "bell": "M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 20h4",
    "share": "M15 5l4 4-4 4M19 9H9a4 4 0 0 0-4 4v6",
}
_KEYS = {"name", "title", "version", "requires_api", "kind", "description", "capabilities", "slots", "binaries",
         "env", "entry", "settings_schema", "menu", "actions", "remote_humans", "ticket_options"}
_REQUIRED = ("name", "title", "version", "requires_api", "kind", "capabilities", "entry")


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    type: str
    options: tuple[str, ...] = ()
    default: object = None


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    confirm: str | None = None
    tickets: bool = False  # true: act() may return close/reopen/import intents for its target
    accepts_file: tuple | None = None  # (max_bytes, types): act() gets upload=Upload; types lower-case, () = any


@dataclass(frozen=True)
class TicketOption:
    """A yes/no choice an addon adds to every ticket: core draws it on the new-ticket form, on the approve card and on
    the ticket page, keeps the value per ticket in the addon's own state, and only a human sets it."""
    id: str
    label: str
    help: str = ""
    default: bool = False


MAX_TICKET_OPTIONS = 3


@dataclass(frozen=True)
class Manifest:
    name: str
    title: str
    version: str
    requires_api: str
    kind: str
    capabilities: frozenset
    entry: str
    description: str = ""
    slots: tuple[str, ...] = ()
    binaries: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    settings_schema: tuple[SettingField, ...] = ()
    menu: dict | None = None
    actions: tuple[ActionSpec, ...] = ()
    remote_humans: bool = False
    ticket_options: tuple[TicketOption, ...] = ()

    @property
    def entry_module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def entry_package(self) -> str:
        return self.entry_module.split(".", 1)[0]

    @property
    def entry_factory(self) -> str:
        return self.entry.split(":", 1)[1]

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def field(self, key: str) -> SettingField | None:
        return next((f for f in self.settings_schema if f.key == key), None)

    def ticket_option(self, option_id: str) -> TicketOption | None:
        return next((o for o in self.ticket_options if o.id == option_id), None)

    def action(self, action_id: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.id == action_id), None)

    def bare_binaries(self) -> tuple[str, ...]:
        return tuple(b for b in self.binaries if not b.startswith("setting:"))

    def setting_binaries(self) -> tuple[str, ...]:
        return tuple(b.removeprefix("setting:") for b in self.binaries if b.startswith("setting:"))

    def defaults(self) -> dict:
        return {f.key: f.default for f in self.settings_schema if f.default is not None}

    def permissions(self) -> dict:
        return {"capabilities": sorted(self.capabilities), "binaries": list(self.binaries), "env": list(self.env),
                "requires_api": self.requires_api, "actions": sorted(a.id + (" (tickets)" if a.tickets else "") for a in self.actions),
                "uploads": sorted(a.id for a in self.actions if a.accepts_file),
                "remote_humans": self.remote_humans}


def _str_list(data: dict, key: str, problems: list[str]) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{key} must be a list of strings")
        return []
    return value


def _fields(data: dict, problems: list[str]) -> list[SettingField]:
    raw = data.get("settings_schema", [])
    if not isinstance(raw, list):
        problems.append("settings_schema must be a list of fields")
        return []
    out, seen = [], set()
    for i, f in enumerate(raw):
        where = f"settings_schema[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{where} must be an object")
            continue
        unknown = set(f) - {"key", "label", "type", "options", "default", "secret"}
        if unknown:
            problems.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        key, label, ftype = f.get("key"), f.get("label"), f.get("type")
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key in seen:
            problems.append(f"{where}.key must be a unique name like 'greeting'")
            continue
        seen.add(key)
        if not isinstance(label, str) or not label.strip():
            problems.append(f"{where}.label must be a non-empty string")
        if ftype not in FIELD_TYPES:
            problems.append(f"{where}.type must be one of {', '.join(sorted(FIELD_TYPES))}")
        if f.get("secret") not in (None, False):
            problems.append(f"{where}: secret fields are not allowed; secrets stay in the tool's own login")
        options = f.get("options", [])
        if ftype == "select" and (not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options)):
            problems.append(f"{where}: a select needs options (a list of strings)")
            options = []
        out.append(SettingField(key, str(label or key), str(ftype), tuple(options) if ftype == "select" else (), f.get("default")))
    return out


def _accepts_file(spec) -> tuple | None:
    """(max_bytes, types) for a valid accepts_file object, else None."""
    if not isinstance(spec, dict) or set(spec) - {"max_bytes", "types"}:
        return None
    mb, types = spec.get("max_bytes"), spec.get("types", [])
    if type(mb) is not int or not 1 <= mb <= MAX_UPLOAD_BYTES or not isinstance(types, list) \
            or not all(isinstance(t, str) and _TYPE.fullmatch(t.lower()) for t in types):
        return None
    return mb, tuple(t.lower() for t in types)


def _actions(data: dict, problems: list[str]) -> list[ActionSpec]:
    raw = data.get("actions", [])
    if not isinstance(raw, list):
        problems.append("actions must be a list")
        return []
    out = []
    for i, a in enumerate(raw):
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not _KEY.fullmatch(a["id"]) \
                or not isinstance(a.get("label"), str) or not a["label"].strip() \
                or not isinstance(a.get("confirm", None), (str, type(None))) \
                or not isinstance(a.get("tickets", False), bool) \
                or set(a) - {"id", "label", "confirm", "tickets", "accepts_file"}:
            problems.append(f"actions[{i}] must be {{id: 'rerun', label: 'Rerun failed', confirm: optional text, "
                            f"tickets: optional bool, accepts_file: optional}}")
            continue
        accepts = None
        if "accepts_file" in a:
            accepts = _accepts_file(a["accepts_file"])
            if accepts is None:
                problems.append(f"actions[{i}].accepts_file must be {{max_bytes: 1..{MAX_UPLOAD_BYTES}, "
                                f"types: optional list like ['.pdf', 'image/png']}}")
                continue
        out.append(ActionSpec(a["id"], a["label"], a.get("confirm"), a.get("tickets", False), accepts))
    return out


def _ticket_options(data: dict, problems: list[str]) -> list[TicketOption]:
    raw = data.get("ticket_options", [])
    if not isinstance(raw, list):
        problems.append("ticket_options must be a list")
        return []
    if len(raw) > MAX_TICKET_OPTIONS:
        problems.append(f"ticket_options: at most {MAX_TICKET_OPTIONS} options")
    out, seen = [], set()
    for i, o in enumerate(raw[:MAX_TICKET_OPTIONS]):
        where = f"ticket_options[{i}]"
        if not isinstance(o, dict) or set(o) - {"id", "label", "help", "default"}:
            problems.append(f"{where} must be {{id: 'notify', label: 'Notify my phone', help: optional text, default: optional bool}}")
            continue
        oid, label, help_, default = o.get("id"), o.get("label"), o.get("help", ""), o.get("default", False)
        if not isinstance(oid, str) or not _KEY.fullmatch(oid) or oid in seen:
            problems.append(f"{where}.id must be a unique name like 'notify'")
            continue
        seen.add(oid)
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            problems.append(f"{where}.label must be a non-empty string of at most 80 characters")
            continue
        if not isinstance(help_, str) or len(help_) > 240:
            problems.append(f"{where}.help must be a string of at most 240 characters")
            continue
        if not isinstance(default, bool):
            problems.append(f"{where}.default must be true or false")
            continue
        out.append(TicketOption(oid, label.strip(), help_.strip(), default))
    return out


def manifest_problems(data) -> list[str]:
    if not isinstance(data, dict):
        return ["the manifest must be a JSON object"]
    problems: list[str] = []
    unknown = set(data) - _KEYS
    if unknown:
        problems.append(f"unknown key(s): {', '.join(sorted(unknown))}")
    for key in _REQUIRED:
        if key not in data:
            problems.append(f"missing required key {key!r}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        problems.append("name must match ^[a-z][a-z0-9-]*$ (lowercase letters, digits and -)")
    elif name in RESERVED_NAMES:
        problems.append(f"name {name!r} is reserved (not one of {', '.join(sorted(RESERVED_NAMES))})")
    if not isinstance(data.get("title", ""), str) or not str(data.get("title", "x")).strip():
        problems.append("title must be a non-empty string")
    if not isinstance(data.get("description", ""), str):
        problems.append("description must be a string")
    if not isinstance(data.get("version"), str) or not _SEMVER.fullmatch(data["version"]):
        problems.append("version must be semver, e.g. 0.1.0")
    if data.get("requires_api") != ADDON_API:
        problems.append(f"requires_api {data.get('requires_api')!r} is not supported (this orch-core supports {ADDON_API!r})")
    if data.get("kind") != "in-process":
        problems.append("kind must be 'in-process'")
    caps = _str_list(data, "capabilities", problems)
    bad = sorted(set(caps) - CAPABILITIES)
    if bad:
        problems.append(f"capabilities: unknown {', '.join(bad)} (allowed: {', '.join(sorted(CAPABILITIES))})")
    slots = _str_list(data, "slots", problems)
    bad = sorted(set(slots) - SLOT_NAMES)
    if bad:
        problems.append(f"slots: unknown {', '.join(bad)} (allowed: {', '.join(sorted(SLOT_NAMES))})")
    if slots and "panel" not in caps:
        problems.append("slots need the 'panel' capability")
    if "panel" in caps and not slots:
        problems.append("the 'panel' capability needs at least one entry in slots")
    fields = _fields(data, problems)
    if ("settings" in caps) != bool(fields):
        problems.append("the 'settings' capability and a non-empty settings_schema go together")
    for b in _str_list(data, "binaries", problems):
        if b.startswith("setting:"):
            key = b.removeprefix("setting:")
            if not any(f.key == key and f.type == "text" for f in fields):
                problems.append(f"binaries: {b} needs a text field {key!r} in settings_schema")
        elif not _BINARY.fullmatch(b):
            problems.append(f"binaries: {b!r} must be a bare command name like 'gh' (no path)")
    for e in _str_list(data, "env", problems):
        if not _ENV.fullmatch(e):
            problems.append(f"env: {e!r} must be an environment variable name like GH_HOST")
        elif e.startswith("ORCH_"):
            problems.append(f"env: {e!r} must not start with ORCH_ (reserved for orch itself)")
    entry = data.get("entry")
    if not isinstance(entry, str) or not _ENTRY.fullmatch(entry):
        problems.append("entry must be 'package.module:factory', e.g. 'hello_status:create'")
    menu = data.get("menu")
    if "page" in caps and menu is None:
        problems.append("the 'page' capability needs menu {title, icon}")
    if menu is not None:
        if not isinstance(menu, dict) or not isinstance(menu.get("title"), str) or not menu["title"].strip() \
                or set(menu) - {"title", "icon"}:
            problems.append("menu must be {title: text, icon: name}")
        elif menu.get("icon", "box") not in MENU_ICONS:
            problems.append(f"menu.icon must be one of {', '.join(sorted(MENU_ICONS))}")
    _actions(data, problems)
    _ticket_options(data, problems)
    if "remote_humans" in data and not isinstance(data["remote_humans"], bool):
        problems.append("remote_humans must be true or false")
    elif data.get("remote_humans") is True and "decisions" not in caps:
        problems.append("remote_humans needs the 'decisions' capability")
    return problems


def parse_manifest(data, where: str = MANIFEST_NAME) -> Manifest:
    problems = manifest_problems(data)
    if problems:
        raise ValidationError(f"{where}: " + "; ".join(problems))
    sink: list[str] = []
    menu = data.get("menu")
    return Manifest(
        name=data["name"], title=data["title"].strip(), version=data["version"], requires_api=data["requires_api"],
        kind=data["kind"], capabilities=frozenset(data["capabilities"]), entry=data["entry"],
        description=data.get("description", ""), slots=tuple(data.get("slots", [])),
        binaries=tuple(data.get("binaries", [])), env=tuple(data.get("env", [])),
        settings_schema=tuple(_fields(data, sink)),
        menu={"title": menu["title"].strip(), "icon": menu.get("icon", "box")} if menu else None,
        actions=tuple(_actions(data, sink)),
        remote_humans=bool(data.get("remote_humans", False)),
        ticket_options=tuple(_ticket_options(data, sink)),
    )


def load_manifest(folder: Path) -> Manifest:
    path = Path(folder) / MANIFEST_NAME
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ValidationError(f"no {MANIFEST_NAME} in {folder}") from None
    except OSError as e:
        raise ValidationError(f"{path}: cannot be read ({e})") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValidationError(f"{path}: not valid JSON ({e})") from e
    return parse_manifest(data, where=str(path))
