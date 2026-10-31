"""``orch host prune --hub-order``: the hub's one-shot removal of itself (#1463).

The hub writes ``<hub state>/remove-order.json`` (fixed path, mode 0600, in its 0700 state folder) and starts
``<abs orch> host prune --hub-order``. The flag takes no path and the order carries no path: prune finds the hub from
its own install record and removes exactly the hub, relay and publish parts of THIS install (their service, their
folder under ``install/parts``, their launcher, program and log) and nothing else. Before the first change prune does
every check (``--dry-run`` stops after them); a refusal writes ``<hub state>/remove-result.json`` and exits non-zero.
The order is spent by a rename and its ``removal_id`` goes to ``~/orch-hub-removed.log`` (outside the removed tree)
before anything is stopped. Kept hub backups move to ``~/orch-hub-backup-<hub8>-<utc>/`` before the trees go.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .execute import execute
from .inventory import dev_markers, gather, install_runner
from .model import Ctx, Inventory, Item, PruneError
from .safe import agent_reason

__all__ = ["main", "ORDER_NAME", "USED_NAME", "RESULT_NAME", "LEDGER_NAME"]

ORDER_NAME = "remove-order.json"
USED_NAME = "remove-order.used"
RESULT_NAME = "remove-result.json"
LEDGER_NAME = "orch-hub-removed.log"
PARTS = ("hub", "relay", "publish")  # also the stop order: the hub first
MAX_ORDER = 4096
MAX_AGE_MS = 10 * 60 * 1000
MAX_FUTURE_MS = 300 * 1000
WAIT_S = 3.0
_HEX32 = re.compile(r"[0-9a-f]{32}")
_KEYS = {"v", "kind", "hub_id", "removal_id", "state_dir", "issued_ms", "exp_ms", "dev"}

#: test seam: how a backup file moves (``os.rename``); a different file system raises EXDEV and the copy path runs
_rename: Callable[[str, str], None] = os.rename


class Refused(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def _now_ms(ctx: Ctx) -> int:
    return int(ctx.clock() * 1000)


# ----------------------------------------------------------------------------------------------- the order file


def _read_fd(fd: int, limit: int) -> bytes:
    data = b""
    while len(data) <= limit:
        chunk = os.read(fd, limit + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _open_dir(path: Path, ctx: Ctx) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Refused("no_order", "There is no removal order to carry out.") from None
    except OSError:
        raise Refused("bad_folder", "The hub folder is not a plain folder (a link or unreadable).") from None
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != ctx.euid or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise Refused("bad_folder", "The hub folder is not private (it must be mode 0700 and yours).")
    return fd


def _open_file(dfd: int, name: str, ctx: Ctx, limit: int, code: str) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        raise Refused("no_order" if code == "bad_order" else code, f"{name} is missing.") from None
    except OSError:
        raise Refused(code, f"{name} is a link or cannot be opened.") from None
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != ctx.euid
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1
            or st.st_size > limit
        ):
            raise Refused(code, f"{name} is not a private plain file of the right size.")
        data = _read_fd(fd, limit)
        if len(data) > limit:
            raise Refused(code, f"{name} is too large.")
        return data, st
    finally:
        os.close(fd)


def _int(v: Any) -> bool:
    return type(v) is int and v >= 0


def _parse_order(data: bytes) -> dict[str, Any]:
    try:
        o = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("bad_order", "The removal order is not valid.") from None
    if (
        type(o) is not dict
        or set(o) != _KEYS
        or o["v"] != 1
        or o["kind"] != "orch.hub_remove_order.v1"
        or type(o["hub_id"]) is not str
        or type(o["removal_id"]) is not str
        or not _HEX32.fullmatch(o["removal_id"])
        or type(o["state_dir"]) is not str
        or not _int(o["issued_ms"])
        or not _int(o["exp_ms"])
        or o["exp_ms"] > o["issued_ms"] + MAX_AGE_MS
        or type(o["dev"]) is not bool
    ):
        raise Refused("bad_order", "The removal order is not valid.")
    return o


def _hub_meta(dfd: int, ctx: Ctx) -> dict[str, Any]:
    data, _ = _open_file(dfd, "meta.json", ctx, 65536, "bad_hub")
    try:
        m = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("bad_hub", "The hub's own record cannot be read.") from None
    if type(m) is not dict or type(m.get("hub_id")) is not str or type(m.get("dev")) is not bool:
        raise Refused("bad_hub", "The hub's own record cannot be read.")
    return m


# -------------------------------------------------------------------------------------------------- the ledger


def _ledger_path(ctx: Ctx) -> Path:
    return ctx.home / LEDGER_NAME


def _ledger_ids(ctx: Ctx) -> set[str]:
    p = _ledger_path(ctx)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return set()
    except OSError:
        raise Refused("bad_ledger", f"{p} is a link or cannot be read.") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != ctx.euid:
            raise Refused("bad_ledger", f"{p} is not a plain file of yours.")
        ids: set[str] = set()
        for line in _read_fd(fd, 8 << 20).decode("utf-8", "replace").splitlines():
            ids.update(w for w in line.split() if _HEX32.fullmatch(w))
        return ids
    finally:
        os.close(fd)


class Ledger:
    def __init__(self, ctx: Ctx) -> None:
        p = _ledger_path(ctx)
        try:
            self.fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise Refused("bad_ledger", f"{p} cannot be opened for writing (a link?).") from None
        st = os.fstat(self.fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != ctx.euid or st.st_mode & 0o077:
            os.close(self.fd)
            raise Refused("bad_ledger", f"{p} is not a private file of yours (mode 0600).")
        self.ctx = ctx

    def add(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ctx.clock()))
        os.write(self.fd, f"{stamp} {line}\n".encode())
        os.fsync(self.fd)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)


# -------------------------------------------------------------------------------------------------- the result


def _write_result(hubdir: Path, ctx: Ctx, code: str, message: str) -> None:
    """``remove-result.json`` (0600) in the hub folder, if there is one; never raises. A ``no_order`` run (the job came
    back after the order was used) never replaces the result an earlier run wrote."""
    if code == "no_order" and os.path.lexists(os.path.join(os.path.realpath(hubdir), RESULT_NAME)):
        return
    doc = json.dumps({"ok": False, "code": code, "message_plain": message, "ms": _now_ms(ctx)}) + "\n"
    with contextlib.suppress(OSError):
        d = os.path.realpath(hubdir)
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            tmp = f".{RESULT_NAME}.tmp"
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dfd)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "w") as f:
                f.write(doc)
            os.rename(tmp, RESULT_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)


# ---------------------------------------------------------------------------------------------------- the plan


class Plan:
    def __init__(self) -> None:
        self.order: dict[str, Any] = {}
        self.hubdir = Path()
        self.hub_id = ""
        self.parts: list[str] = []
        self.items: list[Item] = []  # part (service) items, hub first
        self.trees: list[Path] = []
        self.backups: list[str] = []  # file names in <hub state>/backups
        self.backup_src = Path()
        self.backup_dest = Path()
        self.order_st: os.stat_result | None = None


def _exe_paths(ctx: Ctx) -> list[str]:
    cands = [ctx.orch_exe] if ctx.orch_exe else [sys.argv[0], sys.executable]
    out: list[str] = []
    for c in cands:
        if c:
            out += [os.path.abspath(c), os.path.realpath(c)]
    return out


def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip("/") + "/")


def _utc(ctx: Ctx) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ctx.clock()))


def _preflight(ctx: Ctx, dry: bool, plan: Plan | None = None) -> Plan:
    """Every check, nothing changed. Raises :class:`Refused`. ``plan`` is filled as the checks go, so a refusal
    still leaves the order id once it was read."""
    from orch.install.state import Layout, load
    from orch.ops.runtime import state_dir

    plan = plan if plan is not None else Plan()
    why = agent_reason(ctx.env, ctx.euid)
    if why:
        raise Refused("agent", f"Removing the hub is a person's job; an agent never does it ({why}).", 3)
    layout = Layout(ctx.env)
    state = state_dir(ctx.env)
    plan.hubdir = layout.data("hub")
    markers = dev_markers(ctx, state)
    if markers:
        raise Refused("dev_kit", "This looks like the orch dev kit (" + "; ".join(markers) + "); refused here.", 4)
    rec = load(layout) if layout.file.exists() else {}
    parts_rec = rec.get("parts") if isinstance(rec, dict) else None
    if not isinstance(parts_rec, dict) or not isinstance(parts_rec.get("hub"), dict):
        raise Refused("no_hub", "No hub is installed on this computer.")
    plan.parts = [p for p in PARTS if isinstance(parts_rec.get(p), dict)]

    dfd = _open_dir(plan.hubdir, ctx)
    try:
        data, st = _open_file(dfd, ORDER_NAME, ctx, MAX_ORDER, "bad_order")
        plan.order, plan.order_st = _parse_order(data), st
        meta = _hub_meta(dfd, ctx)
    finally:
        os.close(dfd)
    o = plan.order
    plan.hub_id = meta["hub_id"]
    if o["hub_id"] != meta["hub_id"]:
        raise Refused("other_hub", "The order is for another hub.")
    if o["state_dir"] != os.path.realpath(plan.hubdir):
        raise Refused("other_state", "The order is for another hub folder.")
    now = _now_ms(ctx)
    if now >= o["exp_ms"]:
        raise Refused("expired", "The removal order has expired.")
    if o["issued_ms"] > now + MAX_FUTURE_MS:
        raise Refused("bad_order", "The removal order is dated in the future.")
    if o["dev"] != meta["dev"]:
        raise Refused("dev_mismatch", "The order and this hub disagree about being a test hub.")
    if o["removal_id"] in _ledger_ids(ctx):
        raise Refused("replayed", "This removal order was already used.")

    _scope(ctx, plan, layout, rec)
    _backups(ctx, plan)
    return plan


def _scope(ctx: Ctx, plan: Plan, layout: Any, rec: dict[str, Any]) -> None:
    try:
        inv = gather(ctx)
    except Exception as e:  # noqa: BLE001 - a plain refusal, not a traceback
        raise Refused("inventory", f"Could not look at this computer: {type(e).__name__}: {e}") from None
    by_part = {
        i.data["part"]: i for i in inv.items if i.kind == "part" and i.data.get("part") in PARTS and i.data.get("part")
    }
    plan.items = [by_part[p] for p in plan.parts if p in by_part]
    if "hub" not in by_part:
        raise Refused("no_hub", "The hub service is not in this computer's inventory.")
    try:
        for p in plan.parts:
            runner = install_runner(ctx, layout, (rec["parts"][p] or {}).get("runner"))
            runner.info(p)
    except Exception as e:  # noqa: BLE001
        raise Refused("no_runner", f"The service manager for the hub cannot be reached: {e}") from None
    from orch.install.parts import PARTS as SPECS

    root = layout.root
    cands: list[Path] = []
    for p in plan.parts:
        cands += [root / "parts" / p, root / "bin" / SPECS[p].binary, root / "tools" / SPECS[p].binary]
        cands += [root / "logs" / f"{p}.log"]
    plan.trees = [c for c in cands if os.path.lexists(c)]
    real = [os.path.realpath(c) for c in plan.trees]
    for exe in _exe_paths(ctx):
        if any(_under(exe, r) for r in real + [os.fspath(c) for c in plan.trees]):
            raise Refused("own_program", "The orch program running this lies inside what would be removed.")


def _backups(ctx: Ctx, plan: Plan) -> None:
    plan.backup_src = plan.hubdir / "backups"
    try:
        st = os.lstat(plan.backup_src)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("bad_backups", "The hub's backup folder is a link or not a folder.")
    with os.scandir(plan.backup_src) as it:
        for e in it:
            if e.name.endswith(".orchbak"):
                if not e.is_file(follow_symlinks=False):
                    raise Refused("bad_backups", f"{e.name} in the backup folder is not a plain file.")
                plan.backups.append(e.name)
    plan.backups.sort()
    if not plan.backups:
        return
    plan.backup_dest = ctx.home / f"orch-hub-backup-{plan.hub_id[:8]}-{_utc(ctx)}"
    if os.path.lexists(plan.backup_dest):
        raise Refused("backup_exists", "The folder for the kept backups already exists.")
    if not os.access(ctx.home, os.W_OK | os.X_OK):
        raise Refused("backup_dest", "The home folder cannot be written, so the backups cannot be kept.")


# ------------------------------------------------------------------------------------------------ the removal


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _move_backups(ctx: Ctx, plan: Plan, ledger: Ledger) -> None:
    try:
        with os.scandir(plan.backup_src) as it:  # a backup written after the pre-flight must be kept too
            now_names = [e.name for e in it if e.name.endswith(".orchbak") and e.is_file(follow_symlinks=False)]
    except FileNotFoundError:
        now_names = []
    plan.backups = sorted(set(plan.backups) | set(now_names))
    if not plan.backups:
        return
    dest = plan.backup_dest or ctx.home / f"orch-hub-backup-{plan.hub_id[:8]}-{_utc(ctx)}"
    plan.backup_dest = dest
    os.mkdir(dest, 0o700)  # fails when it exists
    ledger.add(f"backup folder {dest}")
    for name in plan.backups:
        src, dst = os.fspath(plan.backup_src / name), os.fspath(dest / name)
        try:
            _rename(src, dst)
        except OSError as e:
            import errno

            if e.errno != errno.EXDEV:
                raise
            tmp = dst + ".part"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
                for blk in iter(lambda: fin.read(1 << 20), b""):
                    fout.write(blk)
                fout.flush()
                os.fsync(fout.fileno())
            if _sha(src) != _sha(tmp):
                os.unlink(tmp)
                raise PruneError(f"the copy of {name} does not match; the backup was not moved") from None
            os.rename(tmp, dst)
            os.unlink(src)
        ledger.add(f"kept backup {name}")
    dfd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _consume(ctx: Ctx, plan: Plan) -> None:
    dfd = _open_dir(plan.hubdir, ctx)
    try:
        os.rename(ORDER_NAME, USED_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        st = os.stat(USED_NAME, dir_fd=dfd, follow_symlinks=False)
        want = plan.order_st
        if want is None or (st.st_dev, st.st_ino) != (want.st_dev, want.st_ino):
            raise Refused("bad_order", "The removal order changed while it was being checked.")
    except FileNotFoundError:
        raise Refused("no_order", "The removal order disappeared.") from None
    finally:
        os.close(dfd)


def _drop_from_record(ctx: Ctx, plan: Plan) -> None:
    from orch.install.state import Layout, load, save

    layout = Layout(ctx.env)
    st = load(layout)
    parts = st.get("parts")
    if isinstance(parts, dict):
        for p in plan.parts:
            parts.pop(p, None)
    save(layout, st)


def _remove(ctx: Ctx, plan: Plan, ledger: Ledger) -> None:
    from orch.ops.runtime import state_dir

    state = state_dir(ctx.env)
    inv = Inventory(state=state, home=ctx.home, roots=[state])
    inv.idents = {}
    try:
        st = os.lstat(state)
        inv.idents[state] = (st.st_dev, st.st_ino)
    except OSError:
        pass
    ctx.deny = [state]
    ctx.sleep(WAIT_S)
    res = execute(ctx, inv, list(plan.items))
    for it in res.done:
        ledger.add(f"stopped {it.data.get('part', it.label)} service")
    if res.failed is not None:
        raise PruneError(res.error or "a service could not be stopped")
    _move_backups(ctx, plan, ledger)
    steps = [Item("hub", "tree", "part folder or program", os.fspath(t)) for t in plan.trees]
    res = execute(ctx, inv, steps)
    for it in res.done:
        ledger.add(f"removed {it.path}")
    for it, why, _fix in res.skipped:
        ledger.add(f"NOT removed {it.path}: {why[:160]}")
    _drop_from_record(ctx, plan)
    if res.skipped:
        raise PruneError("some files of the hub could not be removed; see ~/orch-hub-removed.log")
    ledger.add("done")


# ---------------------------------------------------------------------------------------------------- main


LAUNCHD_LABEL = "io.orch.hub-remove."


def _launchd_forget(ctx: Ctx, order_id: str | None) -> None:
    """macOS: a job made with ``launchctl submit`` stays and runs again after a failure. On every path of a real run the
    job is removed (best effort; ``removal_id`` is hex, so the label is built from nothing else)."""
    if ctx.platform != "darwin" or not order_id or not all(c in "0123456789abcdef" for c in order_id):
        return
    with contextlib.suppress(Exception):
        ctx.exec(["launchctl", "remove", LAUNCHD_LABEL + order_id])


def main(ctx: Ctx, *, dry_run: bool) -> int:
    plan = Plan()
    try:
        return _main(ctx, dry_run, plan)
    finally:
        if not dry_run:
            _launchd_forget(ctx, (plan.order or {}).get("removal_id") if isinstance(plan.order, dict) else None)


def _main(ctx: Ctx, dry_run: bool, plan: Plan) -> int:
    try:
        from orch.install.state import Layout

        hubdir = Layout(ctx.env).data("hub")
    except Exception:  # noqa: BLE001
        hubdir = ctx.home / ".nothing"
    try:
        plan = _preflight(ctx, dry_run, plan)
        if dry_run:
            ctx.say("orch host prune --hub-order --dry-run: every check passed; nothing was changed.")
            return 0
        ledger = Ledger(ctx)
        try:
            ledger.add(f"removal {plan.order['removal_id']} hub {plan.hub_id[:8]}")  # before the first change
            _consume(ctx, plan)
            try:
                _remove(ctx, plan, ledger)
            except (PruneError, OSError) as e:
                ledger.add(f"FAILED {e}")
                _write_result(plan.hubdir, ctx, "remove_failed", f"Removing the hub stopped: {e}")
                ctx.err.write(f"orch host prune --hub-order: {e}\n")
                return 1
        finally:
            ledger.close()
        ctx.say("The hub is removed.")
        return 0
    except Refused as r:
        if not dry_run:
            _write_result(hubdir, ctx, r.code, str(r))
        ctx.err.write(f"orch host prune --hub-order: {r} ({r.code})\n")
        return r.exit_code
