"""``orch host prune``, step 1: find everything orch left on THIS computer. Reads only; nothing is changed.

Sources, all of them recorded by orch itself: the install record (``<state>/install/install.json``), the host registry
(``<state>/host/workspaces.json``), the per-workspace pins (``<state>/hosts/<id>/git-pins.json``), the unit files orch
writes (named by the constants of the modules that write them), the state and release folders, Claude Code's
``.claude.json`` (only the entries the runner wrote for ticket work folders) and, on Linux, the accounts of
:mod:`orch.setup.osusers`. A folder that is not recorded is never listed for removal.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orch.layout import STORE_DIR, WORK_DIR

from .model import Ctx, Inventory, Item, WorkspaceInv, measure

__all__ = ["dev_markers", "edit_text", "gather", "OS_ROOT_PATHS"]

G_SERVICES = "Running services and login items"
G_PARTS = "Installed parts (hub, relay, publish, dashboard)"
G_STATE = "Settings, records and state of this computer"
G_KEYS = "Keys"
G_PROGRAM = "Program copies"
G_CLAUDE = "Claude Code trust entries"
G_OS = "Accounts and system files (need an administrator)"
G_TOOL = "The orch tool itself"

#: what each top-level entry of the state folder is (the label shown); anything else is listed as "other"
STATE_LABELS = {
    "host": "host registry, security log, jobs",
    "hosts": "per-workspace host state",
    "person": "your device key and the records log",
    "land": "landing keys",
    "runner": "agent runner state",
    "run": "sockets of the running host",
    "dashboard": "dashboard data (passkeys, login state)",
    "setup": "setup page state",
    "addon-env": "environments of installed add-ons",
    "records.json": "records tips",
    "person-key": "your person key file",
}
#: the root-owned files orch's setup writes on Linux (osusers, osapply, osfront, oslauncher); macOS helper dirs
OS_ROOT_PATHS = {
    "linux": (
        "/etc/orch",
        "/etc/tmpfiles.d/orch.conf",
        "/etc/tmpfiles.d/orch-front.conf",
        "/etc/systemd/system/orch-runner@.service",
        "/etc/systemd/system/orch-addon@.service",
        "/etc/systemd/system/orch-front.service",
        "/etc/ssh/sshd_config.d/10-orch-agents.conf",
        "/usr/lib/orch",
        "/var/lib/orch",
        "/var/lib/orch-front",
        "/run/orch",
    ),
    "darwin": ("/Library/Application Support/orch", "/Users/Shared/orch"),
}
_ACCOUNT = re.compile(r"[0-9a-f]{32}/d_[0-9a-f]{32}")
_WS_ID = re.compile(r"[0-9a-f]{32}")
_NOT_REMOVABLE = (
    "browser data: the dashboard's cookies, saved passkeys and local storage in your browsers (out of reach)",
    "git history in workspace folders: the empty first commit and any branches orch made are your history",
    "Claude Code's own records of past sessions (~/.claude) and shell history",
    "NetBird and other tools orch only talked to (not installed by orch)",
    "the PATH line that `uv tool update-shell` may have added to your shell file (uv's, not orch's)",
    "copies in backups (Time Machine, snapshots) of anything removed here",
)


def dev_markers(ctx: Ctx, state: Path) -> list[str]:
    """Why this looks like the orch dev kit (prune refuses there unless ``--i-know-dev``)."""
    why = [f"{k} is set" for k in ("ORCH_DEV", "ORCH_DEV_KIT", "ORCH_KEYCHAIN") if ctx.env.get(k)]
    if os.path.lexists(state / "host" / "dev.json"):
        why.append(f"{state}/host/dev.json exists (a dev sandbox)")
    return why


def _entries(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _is_link(path: str | os.PathLike[str]) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def _tree_item(group: str, label: str, path: Path, note: str = "") -> Item:
    files, size = measure(path)
    return Item(group, "tree", label, os.fspath(path), files, size, note)


def _read_json(path: Path, limit: int = 8 << 20) -> Any:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


# ------------------------------------------------------------------------------------------------------------ gather


def gather(ctx: Ctx, extra_workspaces: Iterable[str] = ()) -> Inventory:
    from orch.ops.runtime import state_dir

    state = state_dir(ctx.env)
    home = ctx.home
    inv = Inventory(state=state, home=home)
    inv.roots.append(state)
    inv.not_removable.extend(_NOT_REMOVABLE)
    _services(ctx, inv)
    _state(ctx, inv)
    _releases(ctx, inv)
    registry = _registry(state)
    ws_roots = _workspace_roots(ctx, inv, registry, extra_workspaces)
    for root, wid in ws_roots:
        inv.workspaces.append(_workspace(ctx, inv, root, wid))
    _remote(ctx, inv, registry)
    _claude_trust(ctx, inv)
    _claude_plugin(ctx, inv)
    _keychain(ctx, inv)
    _os_accounts(ctx, inv, registry)
    _tool(ctx, inv)
    return inv


# --------------------------------------------------------------------------------------------------------- services


def install_runner(ctx: Ctx, layout: Any, kind: str | None) -> Any:
    """The runner that started a part: the one its install record names (a part started by the pidfile runner is
    stopped by it, whatever this computer's default is); ``ctx.runner`` when a test or caller fixed one."""
    if ctx.runner is not None:
        return ctx.runner
    from orch.install import service

    cls = {"launchd": service.LaunchdRunner, "systemd": service.SystemdRunner, "pidfile": service.PidfileRunner}.get(
        kind or ""
    )
    return cls(layout) if cls is not None else service.pick_runner(layout, ctx.env)


def _services(ctx: Ctx, inv: Inventory) -> None:
    from orch.host import units as host_units
    from orch.records import timer as records_timer
    from orch.runner import service as runner_service

    home, plat = ctx.home, ctx.platform
    if plat == "darwin":
        agents = home / "Library" / "LaunchAgents"
        units = [
            ("the host service", "launchd", host_units.LAUNCHD_LABEL, agents / f"{host_units.LAUNCHD_LABEL}.plist"),
            (
                "the agent runner service",
                "launchd",
                runner_service.LAUNCHD_LABEL,
                agents / f"{runner_service.LAUNCHD_LABEL}.plist",
            ),
            (
                "the records timer",
                "launchd",
                records_timer.LAUNCHD_LABEL,
                agents / f"{records_timer.LAUNCHD_LABEL}.plist",
            ),
        ]
    elif plat.startswith("linux"):
        d = home / ".config" / "systemd" / "user"
        units = [
            ("the host service", "systemd", host_units.SYSTEMD_NAME, d / host_units.SYSTEMD_NAME),
            ("the agent runner service", "systemd", runner_service.SYSTEMD_NAME, d / runner_service.SYSTEMD_NAME),
            ("the records timer", "systemd", records_timer.SYSTEMD_TIMER, d / records_timer.SYSTEMD_TIMER),
            ("the records job", "systemd", records_timer.SYSTEMD_SERVICE, d / records_timer.SYSTEMD_SERVICE),
        ]
    else:
        units = []
    for label, kind, name, path in units:
        if os.path.lexists(path):
            inv.roots.append(path)
            inv.items.append(Item(G_SERVICES, "unit", label, os.fspath(path), data={"kind": kind, "name": name}))

    from orch.install.state import Layout, load

    layout = Layout(ctx.env)
    if layout.file.exists():
        st = load(layout)
        names = [p for p in ("hub", "relay", "publish") if p in st.get("parts", {})]
        if st.get("dashboard"):
            names.append("dashboard")
        for part in names:
            rec = (st["parts"].get(part) if part != "dashboard" else st["dashboard"]) or {}
            kind = rec.get("runner") if isinstance(rec.get("runner"), str) else None
            try:
                running = "running" if install_runner(ctx, layout, kind).info(part).get("running") else "stopped"
            except Exception:  # noqa: BLE001 - a broken service manager must not hide the rest of the inventory
                running = "unknown"
            inv.items.append(
                Item(
                    G_SERVICES,
                    "part",
                    f"{part} service ({running})",
                    note=str(rec.get("url") or ""),
                    data={"part": part, "service": rec.get("service"), "runner": kind},
                )
            )
    control = inv.state / "run" / "control" / "host.sock"
    if _answers(control):
        inv.warnings.append(
            "a host is running that is not a service (started by hand): prune cannot stop it. Stop it yourself first "
            f"(it answers on {control})"
        )
    sock = inv.state / "dashboard" / "ctl.sock"
    if os.path.lexists(sock):
        inv.items.append(Item(G_SERVICES, "launcher", "the dashboard started by hand", os.fspath(sock)))


# ------------------------------------------------------------------------------------------------------------ state


def _state(ctx: Ctx, inv: Inventory) -> None:
    state = inv.state
    if not os.path.lexists(state):
        return
    if _is_link(state):
        inv.warnings.append(f"{state} is a link: prune does not follow links, so it is left; remove it by hand")
        inv.roots.remove(state)
        return
    default = state.name == "orch"
    for e in _entries(state):
        p = Path(e.path)
        if e.is_symlink():
            inv.warnings.append(f"{p} is a link: left alone (prune never follows links)")
            continue
        if e.name == "install" and e.is_dir(follow_symlinks=False):
            _install_children(inv, p)
            continue
        if e.name == "hosts" and e.is_dir(follow_symlinks=False):
            _hosts_children(inv, p)
            continue
        label = STATE_LABELS.get(e.name)
        if label is None:
            if not default:
                inv.warnings.append(f"{p} is not orch's to remove (a custom state folder holds it): left alone")
                continue
            label = "other orch data"
        inv.items.append(_tree_item(G_STATE, label, p))
    inv.items.append(
        Item(
            G_KEYS,
            "note",
            "device keys: the key file store (One click) and passphrase keys",
            note="deleted with the state folder: person/ and hosts/<workspace>/keys",
        )
    )
    inv.rmdirs.extend([state / "install" / "parts", state / "install", state / "hosts", state])


def _install_children(inv: Inventory, install: Path) -> None:
    for e in _entries(install):
        p = Path(e.path)
        if e.is_symlink():
            inv.warnings.append(f"{p} is a link: left alone")
        elif e.name == "parts" and e.is_dir(follow_symlinks=False):
            for part in _entries(p):
                if part.is_symlink():
                    inv.warnings.append(f"{part.path} is a link: left alone")
                    continue
                inv.items.append(_tree_item(G_PARTS, f"data of {part.name} (its keys and records)", Path(part.path)))
        else:
            label = {
                "tools": "installed programs of the parts",
                "bin": "launchers of the parts",
                "logs": "logs of the parts",
                "install.json": "the install record",
            }.get(e.name, "install data")
            inv.items.append(_tree_item(G_PARTS, label, p))


def _hosts_children(inv: Inventory, hosts: Path) -> None:
    for e in _entries(hosts):
        p = Path(e.path)
        if e.is_symlink():
            inv.warnings.append(f"{p} is a link: left alone")
        elif _WS_ID.fullmatch(e.name):
            inv.items.append(
                _tree_item(G_STATE, f"host state of workspace {e.name[:8]} (keys, requests, hub, publish)", p)
            )
        else:
            inv.items.append(_tree_item(G_STATE, "host-wide secrets and settings", p))


def _releases(ctx: Ctx, inv: Inventory) -> None:
    from orch.host.release import releases_dir

    rel = releases_dir(dict(ctx.env))
    if os.path.lexists(rel):
        inv.roots.append(rel.parent)
        if _is_link(rel):
            inv.warnings.append(f"{rel} is a link: left alone (prune never follows links)")
            return
        inv.items.append(_tree_item(G_PROGRAM, "installed release copies of the host", rel))
        inv.rmdirs.extend([rel, rel.parent])


# -------------------------------------------------------------------------------------------------------- workspaces


def _registry(state: Path) -> list[dict[str, Any]]:
    doc = _read_json(state / "host" / "workspaces.json")
    rows = doc.get("workspaces") if isinstance(doc, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _workspace_roots(
    ctx: Ctx, inv: Inventory, registry: list[dict[str, Any]], extra: Iterable[str]
) -> list[tuple[Path, str | None]]:
    found: dict[str, str | None] = {}
    for r in registry:
        root, wid = r.get("root"), r.get("workspace_id")
        if isinstance(root, str) and root.startswith("/") and isinstance(wid, str) and _WS_ID.fullmatch(wid):
            found.setdefault(os.path.abspath(root), wid)
    rec = _read_json(inv.state / "install" / "install.json")
    for w in (rec.get("workspaces") if isinstance(rec, dict) else None) or []:
        if isinstance(w, dict) and isinstance(w.get("dir"), str) and os.path.isabs(w["dir"]):
            found.setdefault(os.path.abspath(w["dir"]), None)
    for x in extra:
        found.setdefault(os.path.abspath(x), None)
    out = []
    for root, wid in sorted(found.items()):
        p = Path(root)
        if not os.path.lexists(p):
            inv.warnings.append(f"workspace {p} is recorded but the folder is gone")
            continue
        if _is_link(p) or not p.is_dir():
            inv.warnings.append(f"workspace {p} is a link or not a folder: left alone")
            continue
        out.append((p, wid))
    return out


def _skill_items(ctx: Ctx, root: Path) -> Iterable[Item]:
    from orch.instructions import harness as H
    from orch.instructions.skills import builtin_skills

    for skill in builtin_skills():
        for base in (H.SKILLS_DIR, H.CLAUDE_SKILLS_DIR):
            rel = f"{base}/{skill.name}"
            if not os.path.lexists(root / rel):
                continue
            shipped = skill.text
            accepted = (shipped, H._with_header(shipped), H._plain(shipped))
            try:
                why = H._keep_reason(root, rel, False, accepted)
            except (OSError, ValueError):
                why = "unreadable"
            if why:
                ctx_note = f"kept {rel} ({why})"
                yield Item("workspace", "note", ctx_note)
                continue
            names = [f"{rel}/{n.split('/', 1)[1]}" for n in skill.files()]
            names.append(f"{rel}/orch.skill.json")
            yield Item(
                "workspace",
                "skill",
                f"orch skill {skill.name}",
                os.fspath(root / rel),
                data={"root": os.fspath(root), "rel": rel, "files": sorted(set(names))},
            )


def edit_text(what: str, have: str, *, agents_gone: bool = False) -> str | None:
    """A file of the person's WITHOUT orch's lines, or ``None`` when orch has nothing in it. ``""`` means the
    file would be empty (the caller decides whether to delete it)."""
    from orch.instructions import harness as H

    lines = have.split("\n")
    if what == "agents-block":
        begins = [i for i, x in enumerate(lines) if x.strip() == H.BEGIN]
        ends = [i for i, x in enumerate(lines) if x.strip() == H.END]
        if len(begins) == 1 and len(ends) == 1 and begins[0] <= ends[0]:
            out = lines[: begins[0]] + lines[ends[0] + 1 :]
        elif H.LEGACY_AGENTS_POINTER in lines:
            out = [x for x in lines if x != H.LEGACY_AGENTS_POINTER]
        else:
            return None
    elif what == "claude-import":
        out = [x for x in lines if x != H.LEGACY_CLAUDE_IMPORT]
        if agents_gone and [x for x in out if x.strip()] == [H.CLAUDE_IMPORT]:
            out = []
        if out == lines:
            return None
    elif what == "gitignore":
        out = [x for x in lines if x not in (f"{STORE_DIR}/", f"{WORK_DIR}/")]
        if out == lines:
            return None
    else:
        raise ValueError(what)
    while out and not out[-1].strip():
        out.pop()
    # a blank line the block was separated by goes with it
    text = "\n".join(out)
    return (text + "\n") if text.strip() else ""


def _workspace(ctx: Ctx, inv: Inventory, root: Path, wid: str | None) -> WorkspaceInv:
    from orch.instructions import harness as H

    ws = WorkspaceInv(root=root, wid=wid)
    inv.roots.append(root)
    store = root / STORE_DIR
    if os.path.lexists(store):
        if _is_link(store):
            inv.warnings.append(f"{store} is a link: left alone")
        else:
            ws.items.append(_tree_item("workspace", "orch store (tickets, events, keys of the workspace log)", store))
            if wid is None:
                cfg = _read_json(store / "config.json")
                if isinstance(cfg, dict) and isinstance(cfg.get("workspace_id"), str):
                    ws.wid = wid = cfg["workspace_id"] if _WS_ID.fullmatch(cfg["workspace_id"]) else None
    work = root / WORK_DIR
    if os.path.lexists(work):
        if _is_link(work):
            inv.warnings.append(f"{work} is a link: left alone")
        else:
            ws.items.append(
                _tree_item("workspace", "ticket work folders", work, "scratch folders orch made for tickets")
            )
    ws.items.extend(_skill_items(ctx, root))
    agents_gone = False
    for rel, what in (("AGENTS.md", "agents-block"), ("CLAUDE.md", "claude-import"), (".gitignore", "gitignore")):
        p = root / rel
        if _is_link(p) or not os.path.isfile(p):
            continue
        try:
            have = H.safe_read(root, rel) or ""
        except (OSError, ValueError):
            continue
        new = edit_text(what, have, agents_gone=agents_gone)
        if new is None:
            continue
        if what == "agents-block" and new == "":
            agents_gone = True
        verb = "remove orch's lines from" if new else "delete (only orch's lines in it)"
        ws.items.append(
            Item(
                "workspace",
                "edit",
                f"{verb} {rel}",
                os.fspath(p),
                data={"root": os.fspath(root), "rel": rel, "what": what},
            )
        )
    if wid:
        ws.hub = any(os.path.lexists(inv.state / "hosts" / wid / d) for d in ("hub", "membership"))
        for item in _worktrees(inv, wid):
            ws.items.append(item)
    ws.files, ws.size = measure(root)
    home = ctx.home
    if root == home or root in home.parents or len(root.parts) < 3:
        ws.folder_refusal = "this folder is your home folder or contains it"
    elif not os.path.isfile(store / "config.json"):
        ws.folder_refusal = "this folder has no orch store, so prune does not recognise it as an orch workspace"
    return ws


_WT = re.compile(r"(?P<decl>/.+)\.worktrees/(?P<ws8>[0-9a-f]{8})-[A-Z][A-Z0-9]{0,15}-[0-9]{4,}")


def _worktrees(inv: Inventory, wid: str) -> Iterable[Item]:
    doc = _read_json(inv.state / "hosts" / wid / "git-pins.json")
    wts = doc.get("worktrees") if isinstance(doc, dict) else None
    if not isinstance(wts, dict):
        return
    for uid in sorted(wts):
        repos = wts[uid]
        for repo in sorted(repos) if isinstance(repos, dict) else []:
            reg = (repos[repo] or {}).get("reg") if isinstance(repos[repo], dict) else None
            path = reg.get("path") if isinstance(reg, dict) else None
            m = _WT.fullmatch(path) if isinstance(path, str) else None
            if not m or m["ws8"] != wid[:8] or ".." in path.split("/"):
                continue
            inv.roots.append(Path(path))
            yield Item(
                "workspace",
                "worktree",
                f"git worktree of ticket work in {repo}",
                path,
                data={"declared": m["decl"], "path": path},
            )


# ----------------------------------------------------------------------------------------------- remote, claude, os


def _remote(ctx: Ctx, inv: Inventory, registry: list[dict[str, Any]]) -> None:
    hosts = inv.state / "hosts"
    for e in _entries(hosts):
        if not _WS_ID.fullmatch(e.name):
            continue
        w8 = e.name[:8]
        names = {x.name for x in _entries(Path(e.path))}
        if names & {"hub", "membership"}:
            inv.remote.append(
                f"workspace {w8} has a hub membership: the hub still lists it. Run `orch hub leave` in that workspace "
                "first, or ask the hub's admin to remove it (the hub console: Admin > Workspaces > Remove)"
            )
        if "publish" in names:
            inv.remote.append(f"workspace {w8} published shares: they stay on the publish server until it deletes them")
        if any("relay" in n or "phone" in n for n in names):
            inv.remote.append(
                f"workspace {w8} is registered with a relay / paired with a phone: remove the workspace in the phone "
                "app, the relay forgets it when it is idle"
            )
    st = _read_json(inv.state / "install" / "install.json")
    parts = (st.get("parts") if isinstance(st, dict) else None) or {}
    if "hub" in parts:
        inv.remote.append(
            "this computer ran a hub: workspaces on other computers and paired phones still point at it and will "
            "show it as offline; remove it there (orch hub leave) and delete it in the phone app"
        )
    if "relay" in parts:
        inv.remote.append("this computer ran a relay: phones paired through it lose their push channel")
    if "publish" in parts:
        inv.remote.append("this computer ran publish: links to shares that were served from it stop working")
    if (inv.state / "land").is_dir():
        inv.remote.append("a landing deploy key may be registered on a Git server: remove it there if you used it")


def _claude_trust(ctx: Ctx, inv: Inventory) -> None:
    if not inv.workspaces:
        return
    base = ctx.env.get("CLAUDE_CONFIG_DIR")
    cfg = Path(base) / ".claude.json" if base and os.path.isabs(base) else ctx.home / ".claude.json"
    doc = _read_json(cfg)
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        return
    mine = sorted(
        k
        for k in projects
        if isinstance(k, str) and any(k.startswith(f"{ws.root}/{WORK_DIR}/") for ws in inv.workspaces)
    )
    if mine:
        inv.roots.append(cfg)
        inv.items.append(
            Item(
                G_CLAUDE,
                "claude-trust",
                f"trust entries orch added for {len(mine)} ticket work folders",
                os.fspath(cfg),
                data={"cfg": os.fspath(cfg), "keys": mine},
            )
        )


def _claude_plugin(ctx: Ctx, inv: Inventory) -> None:
    """The orch-core plugin in Claude Code (``orch-core@orch-core``), read from Claude's own plugin lists."""
    base = ctx.env.get("CLAUDE_CONFIG_DIR")
    plugins = (Path(base) if base and os.path.isabs(base) else ctx.home / ".claude") / "plugins"
    installed = _read_json(plugins / "installed_plugins.json")
    names = installed.get("plugins") if isinstance(installed, dict) else None
    market = _read_json(plugins / "known_marketplaces.json")
    cmds: list[list[str]] = []
    if isinstance(names, dict) and any(k.startswith("orch-core@") for k in names):
        cmds.append(["claude", "plugin", "uninstall", "orch-core@orch-core"])
    if isinstance(market, dict) and "orch-core" in market:
        cmds.append(["claude", "plugin", "marketplace", "remove", "orch-core"])
    if cmds:
        inv.items.append(
            Item(G_CLAUDE, "plugin", "the orch-core plugin / marketplace entry in Claude Code", data={"cmds": cmds})
        )


def _keychain(ctx: Ctx, inv: Inventory) -> None:
    if ctx.platform != "darwin" or ctx.keychain is None:
        return
    path = ctx.env.get("ORCH_KEYCHAIN") or os.fspath(ctx.home / "Library" / "Keychains" / "login.keychain-db")
    if not os.path.isfile(path):
        return
    try:
        handle = ctx.keychain(path)
        accounts = [a for a in handle.accounts() if _ACCOUNT.fullmatch(a)] if handle else []
    except Exception as e:  # noqa: BLE001 - locked, denied or unavailable: say so, do not guess
        inv.warnings.append(
            f"could not look into the keychain {path} ({e}); if you turned on a keychain key, delete the items "
            "named io.orch.device-key in Keychain Access"
        )
        return
    for acc in accounts:
        inv.items.append(
            Item(
                G_KEYS,
                "keychain",
                "keychain item of a device key (service io.orch.device-key)",
                path,
                data={"account": acc},
            )
        )


def _default_accounts() -> list[tuple[str, str]]:
    try:
        import grp
        import pwd
    except ImportError:  # Windows
        return []
    from orch.setup.osfront import FRONT_USER
    from orch.setup.osusers import AGENT_PREFIX, SLOT_PREFIX

    def ours(n: str) -> bool:
        return n.startswith((AGENT_PREFIX, SLOT_PREFIX)) or n == FRONT_USER

    return [(u.pw_name, "user") for u in pwd.getpwall() if ours(u.pw_name)] + [
        (g.gr_name, "group") for g in grp.getgrall() if ours(g.gr_name)
    ]


def _os_accounts(ctx: Ctx, inv: Inventory, registry: list[dict[str, Any]]) -> None:
    plat = "linux" if ctx.platform.startswith("linux") else ctx.platform
    paths = [p for p in OS_ROOT_PATHS.get(plat, ()) if ctx.root_exists(p)]
    accounts = (ctx.os_accounts or _default_accounts)() if plat == "linux" else []
    groups = [
        r["group"]
        for r in registry
        if isinstance(r.get("group"), str) and re.fullmatch(r"orch-[0-9a-f]{12}", r["group"])
    ]
    if not (paths or accounts):
        return
    cmds: list[str] = []
    for name, kind in accounts:
        if kind != "user":
            continue
        if name.startswith("orch-agent-"):
            cmds.append(f"systemctl disable --now orch-runner@{name.removeprefix('orch-agent-')}.service")
        elif name.startswith("orch-addon-"):
            cmds.append(f"systemctl disable --now orch-addon@{name.removeprefix('orch-addon-')}.service")
        elif name == "orch-front":
            cmds.append("systemctl disable --now orch-front.service")
    for name, kind in accounts:
        if kind == "user":
            inv.items.append(
                Item(
                    G_OS, "os", f"OS user {name}", note="created by orch's admin setup", data={"cmd": ["userdel", name]}
                )
            )
            cmds.append(f"userdel {name}")
    for name, kind in accounts:
        if kind == "group":
            cmds.append(f"groupdel {name}")
    for g in groups:
        cmds.append(f"groupdel {g}")
    for p in paths:
        inv.items.append(Item(G_OS, "os", "system file/folder", p, *measure(p), data={"path": p}))
        cmds.append(f"rm -rf {p}")
    if plat == "linux":
        cmds.append("systemctl daemon-reload")
    inv.root_commands = cmds
    for name, kind in accounts:
        if kind == "group":
            inv.items.append(Item(G_OS, "os", f"OS group {name}", data={"cmd": ["groupdel", name]}))
    for g in groups:
        inv.items.append(Item(G_OS, "os", f"OS group {g}", data={"cmd": ["groupdel", g], "optional": True}))


def _tool(ctx: Ctx, inv: Inventory) -> None:
    rc, out = ctx.run(["uv", "tool", "list"])
    if rc == 0 and any(line.startswith("orch-core ") for line in out.splitlines()):
        inv.items.append(
            Item(G_TOOL, "tool", "the orch command (uv tool orch-core, with the dashboard add-on)", note="removed last")
        )
    else:
        inv.warnings.append("orch is not installed as a uv tool here (nothing to uninstall at the end)")


def _answers(sock: Path) -> bool:
    """Whether a process answers on the unix socket ``sock`` (a stale socket file does not)."""
    import socket

    if not hasattr(socket, "AF_UNIX") or not os.path.lexists(sock):
        return False
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(1.0)
    try:
        probe.connect(os.fspath(sock))
        return True
    except OSError:
        return False
    finally:
        probe.close()
