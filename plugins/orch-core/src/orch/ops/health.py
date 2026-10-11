"""``orch doctor`` and ``orch check`` (C10): verify a workspace without changing it.

The checks open the workspace through :class:`InspectStore`, a read-only :class:`~orch.store.Store` that does not heal:
no host key (so nothing can be appended), no crash recovery, no index rebuild, no genesis pin written. Everything is
replayed with the real verifier (chain, ``host_sig``, person signatures, authorization), so a finding names the log, the
``seq`` and the cause the replay gives. What the files hold is compared with what the log says (projections, external
edits), the signed checkpoints and the host's pins with the logs.

``check`` is the fast subset for a hook or CI (chain, projections, instructions). ``doctor`` is all of it, and with
``--repair`` does the documented safe repairs *through the store*: it answers external edits with the host events
format section 5.8 names, rebuilds the derived index, and removes the key folders of an init that died. It never repairs
a chain, a signature or a checkpoint: only an owner-signed ``restore`` can.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon
from orch.store import Store, StoreError
from orch.store import render as store_render
from orch.store.checkpoints import verify_object
from orch.store.fsio import read_or_none
from orch.store.paths import ULID
from orch.store.pins import HostPins

__all__ = ["Finding", "InspectStore", "check_staged", "inspect", "repair"]

_GRANT = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_SECRET_NAMES = re.compile(r"(?:^|/)(?:dk|pk|wsk)(?:\.key\.json)?$|\.key\.json$|\.p8$|\.pem$|id_(?:rsa|ed25519|ecdsa)$")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" or "warn"
    where: str
    code: str
    what: str
    repair: str | None = None  # the safe repair ``doctor --repair`` does for it, if any

    def line(self) -> str:
        return f"{self.where}: {self.code}: {self.what}"


class InspectStore(Store):
    """A store that only reads: it does not finish a crashed write, does not rebuild the index and cannot append."""

    def _recover_pending(self) -> list[tuple[Path, dict[str, Any]]]:
        return []

    def _finish_pending(self, pend: list[tuple[Path, dict[str, Any]]]) -> None:
        return None

    def _maybe_index(self, state: Any = None) -> None:
        return None


def _open(root: Path, wid: str, state_dir: Path, findings: list[Finding]) -> InspectStore | None:
    pins = HostPins(state_dir, wid)
    try:
        pin = pins.genesis()
    except StoreError as e:
        findings.append(Finding("error", "pin", e.code, e.detail))
        pin = None
    try:
        s = InspectStore.open(
            root, expected_workspace_id=wid, expected_genesis=pin, host=None, host_state_dir=None, load="all"
        )
    except StoreError as e:
        findings.append(Finding("error", "workspace", e.code, e.detail))
        return None
    if pin is None and s.genesis is not None:
        findings.append(
            Finding(
                "warn",
                "pin",
                "pin.missing",
                "the genesis was never pinned on this machine: a swapped workspace would not be noticed",
                None,
            )
        )
    return s


def _label(s: InspectStore, log: str) -> str:
    if log == "workspace":
        return "workspace"
    t = s.state.tickets.get(log)
    return f"{t.key} ({log})" if t else log


def inspect(
    root: Path, wid: str, state_dir: Path, *, fast: bool, instructions: list[dict[str, str]] | None = None
) -> tuple[list[Finding], InspectStore | None]:
    """Every finding about the workspace at ``root``. ``fast`` leaves out the checks that only ``doctor`` makes
    (checkpoint presence, pins, index, repositories, orphan keys)."""
    out: list[Finding] = []
    s = _open(root, wid, state_dir, out)
    if s is None:
        return out, None
    try:
        for r in s.reports:  # what the store met while reading (a torn write, a date from the future)
            out.append(Finding("error", _label(s, r.log) if r.log else "store", r.code, r.detail))
        for log, seq, code, detail in s.chain_errors():
            out.append(Finding("error", f"{_label(s, log)}#{seq}", code, detail))
        for log, d in sorted(s.diverged.items()):
            out.append(Finding("error", f"{_label(s, log)}#{d.seq}", d.code, d.detail))
        core = s.state._core
        for log, lc in sorted(core.logs.items()):
            for i in lc.invalid:
                if i.seq not in lc.acked:
                    out.append(
                        Finding("error", f"{_label(s, log)}#{i.seq}", "auth.invalid_event", f"{i.type}: {i.code}")
                    )
        out += _projections(s)
        out += _checkpoints(s, _has_host_key(state_dir, wid))
        if not fast:
            out += _pins(s, state_dir, wid)
            out += _index(s)
            out += _repos(s, root)
            out += _orphans(state_dir, wid)
        for f in instructions or []:
            out.append(Finding("warn", f["where"], "instructions.stale", f["what"], None))
    except Exception:
        s.close()
        raise
    return out, s


def _projections(s: InspectStore) -> list[Finding]:
    """What the files hold against what the log says: ``ticket.json``, ``body.md``, artifacts, ``config.json``,
    ``keys.jsonl`` (an external edit, a torn write, a file somebody replaced)."""
    out: list[Finding] = []
    root = s.root
    st = s.state
    if st.workspace.genesis is None:
        return out
    pairs = [
        ("config.json", s._config_bytes(st), "projection.config"),
        ("keys.jsonl", s._keys_bytes(), "projection.keys"),
    ]
    for rel, want, code in pairs:
        if read_or_none(root / rel) != want:
            out.append(Finding("error", rel, code, f"{rel} differs from what the log says", "scan"))
    tdir = root / "tickets"
    for p in sorted(tdir.iterdir()) if tdir.is_dir() else []:
        if p.is_dir() and not p.is_symlink() and ULID.fullmatch(p.name):
            log = p / "events.jsonl"
            if not log.is_file() or log.stat().st_size == 0:
                out.append(Finding("error", f"tickets/{p.name}", "ticket.nolog", "a ticket folder with no event log"))
    bad = {c.log for c in st.chain_errors} | {log for log, *_ in s.chain_errors()}
    for uid, view in sorted(st.tickets.items()):
        if uid in bad:
            continue
        where = f"{view.key}"
        tdir = root / "tickets" / uid
        if read_or_none(tdir / "ticket.json") != s._ticket_bytes(uid):
            out.append(
                Finding(
                    "error", where, "projection.ticket", "ticket.json differs from the log (edited by hand)", "scan"
                )
            )
        raw = read_or_none(tdir / "body.md")
        try:
            texts = store_render.parse_body(raw or b"", view.type)
            have = {
                k: canon.section_hash(t) for k, t in texts.items() if canon.section_hash(t) != canon.section_hash("")
            }
        except (store_render.BodyError, canon.HashError):
            out.append(Finding("error", where, "projection.body", "body.md is not a valid body", "scan"))
            continue
        want = {k: v["hash"] for k, v in view.sections.items() if v["hash"] != canon.section_hash("")}
        if have != want:
            names = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
            out.append(
                Finding(
                    "error", where, "projection.body", "body.md differs from the log in " + ", ".join(names), "scan"
                )
            )
        listed = {a.name for a in view.artifacts}
        adir = tdir / "artifacts"
        for f in sorted(adir.iterdir()) if adir.is_dir() and not adir.is_symlink() else []:
            if f.name not in listed:
                out.append(
                    Finding("warn", where, "artifact.unlisted", f"{f.name[:60]} is in artifacts/ but not in the log")
                )
        for a in view.artifacts:
            if a.digest is None:
                continue
            p = tdir / "artifacts" / a.name
            try:
                data = None if p.is_symlink() else read_or_none(p, 256 * 1024 * 1024)
            except OSError:
                data = None
            if data is None:
                out.append(Finding("error", where, "artifact.missing", f"{a.name} is missing or unreadable"))
            elif canon.artifact_digest(data) != a.digest:
                out.append(Finding("error", where, "artifact.mismatch", f"{a.name} differs from the digest in the log"))
    return out


def _has_host_key(state_dir: Path, wid: str) -> bool:
    from orch.custody.file import FILE_SUFFIX
    from orch.custody.files import key_path

    try:
        return key_path(state_dir / "hosts" / wid / "keys", "wsk", FILE_SUFFIX).is_file()
    except Exception:  # noqa: BLE001
        return False


def _checkpoints(s: InspectStore, host_here: bool) -> list[Finding]:
    """Where this machine holds the workspace key it writes a checkpoint after every append, so a missing one means
    somebody deleted it (that hides a rollback): an error. Without the key (a clone) it is only a warning."""
    out: list[Finding] = []
    cps = s._checkpoints
    if s._wsk_pub() is None:
        return out
    lvl = "error" if host_here else "warn"
    if cps.workspace() is None:
        out.append(
            Finding(lvl, "checkpoints", "checkpoint.missing", "no workspace checkpoint: a rollback would not show")
        )
    have = set(cps.ticket_uids())
    missing = sorted(u for u in s.state.tickets if u not in have)
    if missing:
        keys = ", ".join(s.state.tickets[u].key for u in missing[:5])
        out.append(
            Finding(
                "warn", "checkpoints", "checkpoint.missing", f"{len(missing)} tickets have no checkpoint ({keys}...)"
            )
        )
    wsk = s._wsk_pub()
    cdir = s.state_dir / "checkpoints"
    for p in sorted(cdir.glob("*.json")) if cdir.is_dir() else []:
        raw = read_or_none(p)
        try:
            ok = wsk is not None and verify_object(wsk, json.loads(raw or b""))
        except (ValueError, TypeError):
            ok = False
        if not ok:
            out.append(
                Finding("error", f"checkpoints/{p.name}", "checkpoint.bad", "unreadable or not signed by the host key")
            )
    return out


def _pins(s: InspectStore, state_dir: Path, wid: str) -> list[Finding]:
    out: list[Finding] = []
    pins = HostPins(state_dir, wid)
    devices = s.state.workspace.devices
    for r in pins.revocations():
        d = devices.get(r.get("device"))
        if d is None or not d.revoked:
            out.append(
                Finding(
                    "error",
                    f"pins/{str(r.get('device'))[:40]}",
                    "revocation.missing",
                    "the host noted a device revocation that the log does not hold (a rolled-back revocation)",
                )
            )
    return out


def _index(s: InspectStore) -> list[Finding]:
    if s.genesis is None:
        return []
    try:
        ok = s._index.is_current(s._sizes_now(), s.workspace_id, s.genesis)
    except Exception:  # noqa: BLE001 - derived data
        ok = False
    if ok:
        return []
    return [
        Finding("warn", ".state/index.sqlite", "index.stale", "the derived index is missing or out of date", "index")
    ]


def _repos(s: InspectStore, root: Path) -> list[Finding]:
    from orch.store import observe

    out: list[Finding] = []
    for name in sorted(s.state.workspace.repos):
        p = observe.repo_path(root, s.state.workspace.repos, name)
        if p is None or not p.is_dir():
            out.append(Finding("warn", f"repo {name}", "repo.unobservable", "the working copy does not exist"))
        elif observe.head(p) is None:
            out.append(Finding("warn", f"repo {name}", "repo.unobservable", "git cannot read HEAD there"))
    return out


def _orphans(state_dir: Path, wid: str) -> list[Finding]:
    """Key folders of an init that died: the ones ``--repair`` can remove (a marker of a process that is gone)."""
    from orch.ops.workspace_init import MARKER, _alive

    hosts = state_dir / "hosts"
    out: list[Finding] = []
    if not hosts.is_dir():
        return out
    for d in sorted(hosts.iterdir()):
        marker = d / MARKER
        try:
            if d.name == wid or d.is_symlink() or not marker.is_file() or (d / "genesis").exists():
                continue
            pid = int(marker.read_text().strip() or "0")
        except (OSError, ValueError):
            continue
        if pid and not _alive(pid):
            out.append(
                Finding(
                    "warn",
                    f"keys/{d.name[:40]}",
                    "keys.orphan",
                    "keys of an init that died before it finished",
                    "orphans",
                )
            )
    return out


# ---------------------------------------------------------------------------------------------------- repair


def repair(root: Path, wid: str, env: Mapping[str, str], findings: list[Finding], now: Any) -> list[str]:
    """The safe repairs for ``findings``, done through the normal store. Returns what was done."""
    from orch.ops.runtime import Workspace

    did: list[str] = []
    wanted = {f.repair for f in findings if f.repair}
    if not wanted:
        return did
    ws = Workspace(env, now)
    if "scan" in wanted:
        store = ws.store
        events = store.scan()
        did.append(f"answered external edits with {len(events)} host events")
    if "index" in wanted:
        ws.store.rebuild_index()
        did.append("rebuilt the index")
    if "orphans" in wanted:
        from orch.ops.workspace_init import _sweep_orphans

        _sweep_orphans(ws.state_dir)
        did.append("removed the keys of dead inits")
    return did


# ---------------------------------------------------------------------------------------------------- commit check


def _git(root: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=30, check=False, start_new_session=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def check_staged(s: InspectStore, root: Path) -> list[Finding]:
    """What a pre-commit hook refuses: staged files that are not what the log says, a staged log that is not a prefix
    of the verified one, deleted logs, links, anything of ``.state``, key material and grant secrets."""
    out: list[Finding] = []
    raw = _git(root, "diff", "--cached", "--raw", "-z", "--no-renames", "--relative")
    if raw is None:
        return [Finding("error", "git", "git.unavailable", "git cannot list the staged files here")]
    st = s.state
    fields = raw.decode("utf-8", "replace").split("\0")
    entries: list[tuple[str, str, str]] = []  # (status, new mode, path)
    for i in range(0, len(fields) - 1, 2):
        meta = fields[i].lstrip(":").split()
        if len(meta) >= 5:
            entries.append((meta[4], meta[1], fields[i + 1]))
    guarded = re.compile(r"(?:tickets/|events/|\.state(?:/|$))|(?:config|keys)\.jsonl?$")
    for status, mode, rel in sorted(entries, key=lambda e: e[2]):
        if status == "D":
            if rel.endswith(("events.jsonl", "workspace.jsonl", "ticket.json", "body.md", "config.json", "keys.jsonl")):
                out.append(
                    Finding("error", rel, "commit.deleted", "a log or a projection may not be deleted from history")
                )
            continue
        if mode in ("120000", "160000") and guarded.search(rel):
            out.append(Finding("error", rel, "commit.link", "a symlink or submodule where the workspace keeps a file"))
            continue
        blob = _git(root, "show", f":{rel}")
        if blob is None:
            out.append(Finding("error", rel, "git.unavailable", "git cannot read the staged file"))
            continue
        if rel.startswith(".state/") or rel == ".state":
            out.append(
                Finding("error", rel, "commit.state", ".state is local: sessions, intents and keys never go into git")
            )
            continue
        if _SECRET_NAMES.search(rel):
            out.append(Finding("error", rel, "commit.secret", "a key file is never committed"))
            continue
        if _GRANT.search(blob.decode("utf-8", "replace")) or b"ORCH_GRANT=" in blob:
            out.append(Finding("error", rel, "commit.secret", "the file holds a grant secret"))
            continue
        m = re.fullmatch(
            r"tickets/([0-7][0-9A-HJKMNP-TV-Z]{25})/(ticket\.json|body\.md|events\.jsonl|artifacts/.+)", rel
        )
        if rel == "config.json":
            if blob != s._config_bytes(st):
                out.append(Finding("error", rel, "commit.edited", "config.json is not what the log says (hand edit)"))
        elif rel == "keys.jsonl":
            if blob != s._keys_bytes():
                out.append(Finding("error", rel, "commit.edited", "keys.jsonl is not what the log says (hand edit)"))
        elif rel == "events/workspace.jsonl":
            out += _prefix(rel, blob, root / rel, "workspace")
        elif m:
            uid, what = m.groups()
            view = st.tickets.get(uid)
            if view is None:
                out.append(Finding("error", rel, "commit.unknown", "a ticket with no verified log"))
            elif what == "ticket.json" and blob != s._ticket_bytes(uid):
                out.append(Finding("error", rel, "commit.edited", f"{view.key}: ticket.json is not what the log says"))
            elif what == "body.md":
                try:
                    texts = store_render.parse_body(blob, view.type)
                except store_render.BodyError:
                    texts = None
                empty = canon.section_hash("")
                have = {k: canon.section_hash(t) for k, t in (texts or {}).items() if canon.section_hash(t) != empty}
                want = {k: v["hash"] for k, v in view.sections.items() if v["hash"] != empty}
                if texts is None or have != want:
                    out.append(Finding("error", rel, "commit.edited", f"{view.key}: body.md is not what the log says"))
            elif what == "events.jsonl":
                out += _prefix(rel, blob, root / rel, view.key)
            elif what.startswith("artifacts/"):
                name = what[len("artifacts/") :]
                a = next((x for x in view.artifacts if x.name == name), None)
                if a is None or a.digest != canon.artifact_digest(blob):
                    out.append(
                        Finding("error", rel, "commit.edited", f"{view.key}: {name} is not the artifact the log names")
                    )
    return out


def _prefix(rel: str, blob: bytes, disk: Path, label: str) -> list[Finding]:
    """A staged log must be the verified log or an earlier part of it, byte for byte (a forged or rewritten line is
    neither)."""
    cur = read_or_none(disk, 512 * 1024 * 1024)
    if cur is None or not cur.startswith(blob):
        return [Finding("error", rel, "commit.forged", f"{label}: the staged log is not a prefix of the verified log")]
    return []
