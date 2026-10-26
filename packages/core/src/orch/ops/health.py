"""``orch doctor`` and ``orch check`` (C10): verify a workspace without changing it.

The checks open the workspace through :class:`InspectStore`, a read-only :class:`~orch.store.Store` that does not heal:
no host key (so nothing can be appended), no crash recovery, no index rebuild, no genesis pin written. Everything is
replayed with the real verifier (chain, ``host_sig``, person signatures, authorization), so a finding names the log, the
``seq`` and the cause the replay gives. What the files hold is compared with what the log says (projections, external
edits), the signed checkpoints and the host's pins with the logs.

``check`` is the fast subset for a hook or CI (chain, projections, instructions). ``doctor`` is all of it, and with
``--repair`` does the documented safe repairs *through the store*: it answers external edits with the host events
format section 5.8 names and rebuilds the derived index. It never repairs
a chain, a signature or a divergent checkpoint: only an owner-signed ``restore`` can.
A missing ticket checkpoint can be written only for the last recorded append, after replay verifies.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, SubprocessError
from typing import Any

from orch import canon, gitsafe
from orch.layout import STORE_DIR, StorePin
from orch.ops.caps import Capabilities, CapabilityDenied, Files
from orch.store import Store, StoreError
from orch.store import render as store_render
from orch.store.checkpoints import verify_object
from orch.store.paths import ULID
from orch.store.pins import HostPins, is_forwarded, pair
from orch.store.rootfs import RootFS
from orch.store.store import MISSING_CHECKPOINT_RECOVERY

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


def _open(
    root: Path,
    wid: str,
    state_dir: Path,
    findings: list[Finding],
    ident: tuple[int, int] | None = None,
    config: bytes | None = None,
    store_pin: StorePin | None = None,
) -> InspectStore | None:
    pins = HostPins(state_dir, wid)
    try:
        pin = pins.genesis()
    except StoreError as e:
        findings.append(Finding("error", "pin", e.code, e.detail))
        pin = None
    try:
        s = InspectStore.open(
            root,
            expected_workspace_id=wid,
            expected_genesis=pin,
            host=None,
            host_state_dir=None,
            load="all",
            ident=ident,  # the root discovery found, by its fd: every read below goes through it (#516)
            config=config,  # the config.json bytes discovery checked, not a second read by path
            store_pin=store_pin,
        )
    except StoreError as e:
        findings.append(Finding("error", "workspace", e.code, e.detail))
        return None
    findings.extend(_pending_linked(s.fs))
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


def _pending_linked(fs: RootFS) -> list[Finding]:
    """A crashed append of a relay-linked workspace (its pending directory carries an outbox entry, H12b): named, so
    an operator knows why a store without host state refuses to open; the next host open finishes it."""
    from orch.store.outbox import pending_linked_appends

    out = []
    for ev, log in pending_linked_appends(fs):
        what = f"a crashed append of event {ev} to {log} carries a relay outbox entry"
        out.append(Finding("warn", "workspace", "outbox.pending", what + "; the next host open finishes it", None))
    return out


def _label(s: InspectStore, log: str) -> str:
    if log == "workspace":
        return "workspace"
    t = s.state.tickets.get(log)
    return f"{t.key} ({log})" if t else log


def inspect(
    root: Path,
    wid: str,
    state_dir: Path | None,
    *,
    fast: bool,
    instructions: list[dict[str, str]] | None = None,
    caps: Capabilities,
    store: Store | None = None,
    sees: Callable[[Any], bool] | None = None,
    ident: tuple[int, int] | None = None,
    config: bytes | None = None,
    store_pin: StorePin | None = None,
) -> tuple[list[Finding], Store | None]:
    """Every finding about the workspace at ``root``. ``fast`` leaves out the checks that only ``doctor`` makes
    (checkpoint presence, pins, index, repositories).

    Host mode (H4a1) passes ``store`` (the host's own; it is used as it is, never closed here, and no second store is
    opened, no ``config.json`` and no state directory read) and ``sees`` (the viewer's visibility test). Every finding
    about a ticket the viewer may not see, or whose visibility cannot be told (a log that does not replay), is left out
    before anything is counted (F6).

    Direct mode passes ``ident`` (the root's ``st_dev``/``st_ino`` as discovery saw it) and ``config`` (the
    ``config.json`` bytes discovery read from the checked fd): the store opens the root by
    its fd and refuses another directory, and every file the checks read is read below that fd
    (:class:`~orch.store.rootfs.RootFS`, ``O_NOFOLLOW`` on every component), never by path (#516)."""
    out: list[Finding] = []
    hosted = store is not None
    if hosted:
        s: Store | None = store
        store.load_all()
    else:
        assert state_dir is not None
        s = _open(root, wid, state_dir, out, ident, config, store_pin)
    if s is None:
        return out, None
    # direct mode reads the workspace through the store's pinned root; host mode through the host's Files
    files: Files | RootFS = caps.files if caps.files.roots is not None else s.fs

    def ok(log: str | None) -> bool:
        if sees is None or log is None or log == "workspace":
            return True
        view = s.state.tickets.get(log)
        return view is not None and sees(view)

    try:
        for r in s.reports:  # what the store met while reading (a torn write, a date from the future)
            if ok(r.log):
                out.append(Finding("error", _label(s, r.log) if r.log else "store", r.code, r.detail))
        for log, seq, code, detail in s.chain_errors():
            if ok(log):
                out.append(Finding("error", f"{_label(s, log)}#{seq}", code, detail))
        for log, d in sorted(s.diverged.items()):
            if any(broken_log == log for broken_log, *_ in s.chain_errors()) or not ok(log):
                continue
            out.append(Finding("error", f"{_label(s, log)}#{d.seq}", d.code, d.detail))
        core = s.state._core
        for log, lc in sorted(core.logs.items()):
            if not ok(log):
                continue
            for i in lc.invalid:
                if i.seq not in lc.acked:
                    out.append(
                        Finding("error", f"{_label(s, log)}#{i.seq}", "auth.invalid_event", f"{i.type}: {i.code}")
                    )
        out += _projections(s, files, ok)
        host_key = s.can_write if hosted else _has_host_key(state_dir, wid)  # type: ignore[arg-type]
        pins = s._pins if hosted else HostPins(state_dir, wid)  # type: ignore[arg-type]
        out += _checkpoints(s, host_key, files, pins, ok)
        if not fast:
            out += _pins(s, state_dir, wid)
            out += _index(s)
            out += _repos(s, root, caps)
        for f in instructions or []:
            out.append(Finding("warn", f["where"], "instructions.stale", f["what"], None))
    except StoreError as e:  # a directory swapped for a symlink while the checks ran (#516): a finding, not a crash
        out.append(Finding("error", "workspace", e.code, e.detail))
    except Exception:
        if not hosted:
            s.close()
        raise
    return out, s


def _isdir(files: Files | RootFS, path: Path) -> bool:
    """A real directory, opened through the fd walk (never through a link): host mode by the capability, direct mode
    below the store's pinned root (#516)."""
    if isinstance(files, RootFS):
        return files.is_dir(path)
    try:
        files.listdir(path)
    except OSError:
        return False
    return True


def _size(files: Files | RootFS, path: Path) -> int | None:
    """The size of a regular file (``None``: missing, a link, not a regular file), through the fd walk."""
    if isinstance(files, RootFS):
        try:
            st = files.lstat(path)
        except (OSError, StoreError):
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None
    try:
        fd = files.open_fd(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def _read(files: Files | RootFS, path: Path, limit: int | None = None) -> bytes | None:
    """A file's bytes or ``None`` (missing, a symlink, a symlinked directory on the way): host mode through the
    capability, direct mode below the store's pinned root (#516)."""
    if isinstance(files, RootFS):
        try:
            return files.read_or_none(path) if limit is None else files.read_or_none(path, limit)
        except (OSError, StoreError):
            return None
    try:
        return files.read_bytes(path)
    except OSError:
        return None


def _projections(s: Store, files: Files | RootFS, visible: Callable[[str], bool] = lambda log: True) -> list[Finding]:
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
        if _read(files, root / rel) != want:
            out.append(Finding("error", rel, code, f"{rel} differs from what the log says", "scan"))
    tdir = root / "tickets"
    for p in [tdir / n for n in sorted(files.listdir(tdir))] if _isdir(files, tdir) else []:
        if ULID.fullmatch(p.name) and visible(p.name) and _isdir(files, p):
            if not _size(files, p / "events.jsonl"):
                out.append(Finding("error", f"tickets/{p.name}", "ticket.nolog", "a ticket folder with no event log"))
    bad = {c.log for c in st.chain_errors} | {log for log, *_ in s.chain_errors()}
    for uid, view in sorted(st.tickets.items()):
        if uid in bad or not visible(uid):
            continue
        where = f"{view.key}"
        tdir = root / "tickets" / uid
        if _read(files, tdir / "ticket.json") != s._ticket_bytes(uid):
            out.append(
                Finding(
                    "error", where, "projection.ticket", "ticket.json differs from the log (edited by hand)", "scan"
                )
            )
        raw = _read(files, tdir / "body.md")
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
        for f in [adir / n for n in sorted(files.listdir(adir))] if _isdir(files, adir) else []:
            if f.name not in listed:
                out.append(
                    Finding("warn", where, "artifact.unlisted", f"{f.name[:60]} is in artifacts/ but not in the log")
                )
        for a in view.artifacts:
            if a.digest is None:
                continue
            p = tdir / "artifacts" / a.name
            try:
                data = _read(files, p, 256 * 1024 * 1024)  # a symlink reads as absent
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
    except Exception:  # noqa: BLE001 - when it cannot be told, judge at the strict level (a missing checkpoint is an error)
        return True


def _checkpoints(
    s: Store,
    host_here: bool,
    files: Files | RootFS,
    pins: HostPins | None = None,
    visible: Callable[[str], bool] = lambda log: True,
) -> list[Finding]:
    """A missing checkpoint can hide a rollback or follow a crash: an error on the host, a warning on clones."""
    out: list[Finding] = []
    cps = s._checkpoints
    if s._wsk_pub() is None:
        return out
    lvl = "error" if host_here else "warn"
    workspace_missing = cps.workspace() is None  # a restore needs that source: point to its finding instead
    if workspace_missing:
        if host_here and pins is not None and pins.held() is None and pins.checkpoint_evidence():
            msg = (
                "no workspace checkpoint, but this host's state shows checkpoints existed: appends are refused. "
                + MISSING_CHECKPOINT_RECOVERY
            )
        else:
            msg = (
                "no workspace checkpoint: a rollback would not show; on a host without checkpoint evidence the next "
                "append starts a new baseline automatically"
            )
        out.append(Finding(lvl, "checkpoints", "checkpoint.missing", msg))
    have = set(cps.ticket_uids())
    missing = sorted(u for u in s.state.tickets if u not in have and visible(u))
    for repairable in (False, True):
        group = [u for u in missing if (host_here and s.repairable_checkpoint(u)) == repairable]
        if not group:
            continue
        keys = ", ".join(s.state.tickets[u].key for u in group[:5])
        out.append(
            Finding(
                lvl,
                "checkpoints",
                "checkpoint.missing",
                f"{len(group)} tickets have no checkpoint ({keys}...)"
                + (
                    "; doctor --repair can finish the last recorded append"
                    if repairable
                    else "; a rollback is possible; "
                    + ("see the workspace checkpoint finding" if workspace_missing else "use orch restore")
                ),
                "checkpoint" if repairable else None,
            )
        )
    wsk = s._wsk_pub()
    cdir = s.state_dir / "checkpoints"
    names = sorted(files.listdir(cdir)) if _isdir(files, cdir) else []
    for p in [cdir / n for n in names if n.endswith(".json")]:
        uid = p.stem.removeprefix("ticket-")  # ticket checkpoints are ticket-<uid>.json (store.checkpoints)
        if ULID.fullmatch(uid) and not visible(uid):
            continue
        raw = _read(files, p)
        try:
            signed = wsk is not None and verify_object(wsk, json.loads(raw or b""))
        except (ValueError, TypeError):
            signed = False
        if not signed:
            out.append(
                Finding("error", f"checkpoints/{p.name}", "checkpoint.bad", "unreadable or not signed by the host key")
            )
    return out


def _pins(s: Store, state_dir: Path | None, wid: str) -> list[Finding]:
    out: list[Finding] = []
    pins = HostPins(state_dir, wid) if state_dir is not None else s._pins  # host mode: the store's own pins
    if pins is None:
        return out
    accepted = s.state.revocation_evidence()
    for r in pins.revocations():
        if is_forwarded(r):
            continue  # forwarded evidence, not claimed by the log
        entry = accepted.get(pair(r))
        if entry is None or entry["revocation"] != r["revocation"]:
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


def _repos(s: InspectStore, root: Path, caps: Capabilities) -> list[Finding]:
    out: list[Finding] = []
    for name in sorted(s.state.workspace.repos):
        p = caps.git.repo_path(s.layout, s.state.workspace.repos, name)  # host mode: lexical, no I/O
        if caps.files.roots is not None:
            # Host mode: never stat a path outside the root; the reader child opens the repo under its pins.
            try:
                head = caps.git.head(p) if p is not None else None
            except CapabilityDenied:
                head = None
            if head is None:
                out.append(Finding("warn", f"repo {name}", "repo.unobservable", "git cannot read HEAD there"))
            continue
        if p is None or not p.is_dir():
            out.append(Finding("warn", f"repo {name}", "repo.unobservable", "the working copy does not exist"))
        else:
            try:
                head = caps.git.head(p)
            except CapabilityDenied:
                head = None
            if head is None:
                out.append(Finding("warn", f"repo {name}", "repo.unobservable", "git cannot read HEAD there"))
    return out


# ---------------------------------------------------------------------------------------------------- repair


def repair(
    root: Path, wid: str, env: Mapping[str, str], findings: list[Finding], now: Any, caps: Capabilities
) -> list[str]:
    """The safe repairs for ``findings``, done through the normal store. Returns what was done."""
    from orch.ops.runtime import Workspace

    did: list[str] = []
    wanted = {f.repair for f in findings if f.repair}
    if not wanted:
        return did
    ws = Workspace(env, now, files=caps.files)
    if "scan" in wanted:
        store = ws.store
        events = store.scan()
        did.append(f"answered external edits with {len(events)} host events")
    if "checkpoint" in wanted:
        store = ws.store
        store.load_all()
        store.repair_checkpoints()
        did.append("wrote missing ticket checkpoints for verified logs")
    if "index" in wanted:
        ws.store.rebuild_index()
        did.append("rebuilt the index")
    return did


# ---------------------------------------------------------------------------------------------------- commit check


def _gate(root: Path, caps: Capabilities) -> str | None:
    """The invariant of :mod:`orch.gitsafe` on the workspace's repository (#546): why orch does not run git there, or
    ``None``. Where the handler has no process capability (host mode), nothing is checked: :func:`_git` then runs
    nothing either."""
    if not caps.exec.enabled:
        return None
    try:
        gitsafe.gate(os.path.abspath(root), caps.env)
    except gitsafe.Unsafe as exc:
        return exc.reason
    return None


def _git(root: Path, op: str, *, caps: Capabilities, path: str | None = None) -> bytes | None:
    """Operation ``op`` of :data:`orch.gitsafe.OPS` (``staged_raw``, ``staged_blob`` of ``path``), isolated
    (:func:`orch.gitsafe.command`, #546: the gate again, no free argv, system and global configuration off, none of
    the person's configuration, hooks, fsmonitor, network, pager, external diff and textconv off, an environment built
    from scratch), spawned through ``caps.exec``. ``None`` when it is refused or fails; with no process capability
    (host mode) nothing runs."""
    if not caps.exec.enabled:
        return None
    # RACE NOTE (#565): gitsafe.command gates the repo's config and git starts afterwards, so a concurrent writer of
    # .git/config could change it in between. Accepted here only: staged_raw compares the index with HEAD by object id
    # and staged_blob prints a stored blob as stored, so no work-tree file is read and no clean/smudge filter,
    # textconv or diff driver applies; and this runs inside the person's own `git commit` (the pre-commit hook), whose
    # git has already read and acted on that same config as the person. Every other read is orch.gitread's.
    try:
        argv, env = gitsafe.command(os.path.abspath(root), op, caps.env, operand=path)
        done = caps.exec.run(
            argv, capture_output=True, timeout=30, check=False, start_new_session=True, env=env, cwd="/",
            stdin=DEVNULL,
        )  # fmt: skip
    except (gitsafe.Unsafe, OSError, SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


_TICKET_FILE = re.compile(r"tickets/([0-7][0-9A-HJKMNP-TV-Z]{25})/(ticket\.json|body\.md|events\.jsonl|artifacts/.+)")


def check_staged(s: InspectStore, root: Path, caps: Capabilities) -> list[Finding]:
    """What a pre-commit hook refuses: staged files that are not what the log says, a staged log that is not a prefix
    of the verified one, deleted logs, links, anything of ``.state``, key material and grant secrets."""
    out: list[Finding] = []
    refused = _gate(root, caps)
    if refused is not None:
        return [Finding("error", "git", "git.unavailable", f"orch does not run git here: {refused}")]
    raw = _git(root, "staged_raw", caps=caps)
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
    store = STORE_DIR + "/"
    for status, mode, full in sorted(entries, key=lambda e: e[2]):
        # The workspace's own store (``.orch/``) is checked against the log, relative to the store; ``full`` (the path
        # in the repository) names the file in findings and for git. The project's other files are the project's:
        # only the secret checks below apply to them.
        folded_full = full.casefold()
        if ".state" in folded_full.split("/"):  # local state, in any folder and spelling: never committed (as before)
            out.append(Finding("error", full, "commit.state", ".state is local: never commit it"))
            continue
        rel: str | None = None
        if folded_full == STORE_DIR.casefold() or folded_full.startswith(store.casefold()):
            if not full.startswith(store):  # another spelling of the store folder (the same folder on a
                out.append(Finding("error", full, "commit.unknown", "a file the workspace does not keep there"))
                continue  # case-insensitive disk): never examined under another name, so refused
            rel = full[len(store) :]
        if rel is not None:
            folded = rel.casefold()
            protected = folded.startswith(("tickets/", "events/")) or folded in (
                "tickets",
                "events",
                "config.json",
                "keys.jsonl",
            )
            known = rel in ("events/workspace.jsonl", "config.json", "keys.jsonl") or _TICKET_FILE.fullmatch(rel)
            if protected and (not rel.isascii() or not known):
                out.append(Finding("error", full, "commit.unknown", "a file the workspace does not keep there"))
                continue
        if status == "D":
            if rel is None:
                continue
            if rel.endswith(("events.jsonl", "workspace.jsonl", "ticket.json", "body.md", "config.json", "keys.jsonl")):
                out.append(
                    Finding("error", full, "commit.deleted", "a log or a projection may not be deleted from history")
                )
            elif _TICKET_FILE.fullmatch(rel):
                out.append(Finding("error", full, "commit.deleted", "an artifact may not be deleted from history"))
            continue
        if rel is not None and mode in ("120000", "160000") and guarded.search(rel):
            out.append(Finding("error", full, "commit.link", "a symlink or submodule where the workspace keeps a file"))
            continue
        blob = _git(root, "staged_blob", caps=caps, path=full)
        if blob is None:
            out.append(Finding("error", full, "git.unavailable", "git cannot read the staged file"))
            continue
        if _SECRET_NAMES.search(full):
            out.append(Finding("error", full, "commit.secret", "a key file is never committed"))
            continue
        if _GRANT.search(blob.decode("utf-8", "replace")) or b"ORCH_GRANT=" in blob:
            out.append(Finding("error", full, "commit.secret", "the file holds a grant secret"))
            continue
        if rel is None:
            continue
        m = _TICKET_FILE.fullmatch(rel)
        if rel == "config.json":
            if blob != s._config_bytes(st):
                out.append(Finding("error", full, "commit.edited", "config.json is not what the log says (hand edit)"))
        elif rel == "keys.jsonl":
            if blob != s._keys_bytes():
                out.append(Finding("error", full, "commit.edited", "keys.jsonl is not what the log says (hand edit)"))
        elif rel == "events/workspace.jsonl":
            out += _prefix(full, rel, blob, s.fs, "workspace")
        elif rel.startswith(("tickets/", "events/")) and not m:
            out.append(Finding("error", full, "commit.unknown", "a file the workspace does not keep there"))
        elif m:
            uid, what = m.groups()
            view = st.tickets.get(uid)
            if view is None:
                out.append(Finding("error", full, "commit.unknown", "a ticket with no verified log"))
            elif what == "ticket.json" and blob != s._ticket_bytes(uid):
                out.append(Finding("error", full, "commit.edited", f"{view.key}: ticket.json is not what the log says"))
            elif what == "body.md":
                try:
                    texts = store_render.parse_body(blob, view.type)
                except store_render.BodyError:
                    texts = None
                empty = canon.section_hash("")
                have = {k: canon.section_hash(t) for k, t in (texts or {}).items() if canon.section_hash(t) != empty}
                want = {k: v["hash"] for k, v in view.sections.items() if v["hash"] != empty}
                if texts is None or have != want:
                    out.append(Finding("error", full, "commit.edited", f"{view.key}: body.md is not what the log says"))
            elif what == "events.jsonl":
                out += _prefix(full, rel, blob, s.fs, view.key)
            elif what.startswith("artifacts/"):
                name = what[len("artifacts/") :]
                a = next((x for x in view.artifacts if x.name == name), None)
                if a is None or a.digest != canon.artifact_digest(blob):
                    out.append(
                        Finding("error", full, "commit.edited", f"{view.key}: {name} is not the artifact the log names")
                    )
    return out


def _prefix(full: str, rel: str, blob: bytes, fs: RootFS, label: str) -> list[Finding]:
    """A staged log must be the verified log or an earlier part of it, byte for byte (a forged or rewritten line is
    neither). The log on disk is read below the store's pinned root (#516)."""
    try:
        cur = fs.read_or_none(rel, 512 * 1024 * 1024)
    except (OSError, StoreError):
        cur = None
    if cur is None or not cur.startswith(blob):
        return [Finding("error", full, "commit.forged", f"{label}: the staged log is not a prefix of the verified log")]
    return []
