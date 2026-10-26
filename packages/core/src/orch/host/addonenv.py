"""Building, keeping and removing the dependency environment of an add-on, host side (V3 G4, #922; spec 8.2; design
``state/design-g4.md``, conditions C3 and C4).

The signed ``addon.granted`` names ``runtime``; this module makes the environment that grant promised, and only the
host does, only after the signature, only when the person runs ``market.env_build`` (human-only, off the worker):

1. Under the store lock the latest grant is read (enabled, with ``runtime``); then the lock is released.
2. The package of the grant is read into memory from ``addons/<name>`` and its digest must equal the grant's: the
   only source of the lockfile bytes. No tool ever gets a path into the package or into staging.
3. One flock per ``(workspace, name)``; the plan for this computer; every artifact from the cache or from
   :mod:`orch.market.download` (hashed against the lock BEFORE anything opens it).
4. A fresh ``mkdtemp`` build directory (0700, same file system as the target). Python: orch writes ``req.txt`` from the
   checked rows (no author text), copies the verified wheels in, and runs ``pip install --no-index --require-hashes
   --no-deps --only-binary=:all: --target`` with an environment made from nothing. Node: orch unpacks each verified
   tarball itself (:mod:`orch.market.untar`). No dependency code runs, no interpreter starts inside the environment.
5. The post-check (:func:`orch.addons.envtree.walk`): regular files and directories only, one link each, no path
   that leaves the tree, no ``*.pth``/``sitecustomize``/``usercustomize`` at its root, limits. Modes are normalized.
6. Under the store lock and the flock again: the grant must still be the one we built for (else the build is
   discarded, ``superseded``). A target that already exists is NEVER adopted: it is removed through a directory
   descriptor and replaced by ``rename``. Then the stamp is written (0600, atomic).
7. The name's other digest directories and the cache files this build used are removed.

``recover`` runs at start: every ``.build-*`` goes, an unstamped or foreign target is removed, environments of
uninstalled names and of old digests go, cache files older than a day go. The runner never builds; a missing or
changed environment is ``market.env_changed`` and the add-on does not run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orch import oslock
from orch.addons import envtree, install, lockfile
from orch.addons.envtree import TreeError
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.market import ADDONS, MarketError, download, untar
from orch.store.fsio import write_atomic

from .custody import ensure_private_dir

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["build", "env_row", "recover", "remove", "stage_fetch"]

LOG = logging.getLogger("orch.host.addonenv")
PIP_SECONDS = 300
PIP_LOG_MAX = 8 << 20
STAGE_SECONDS = 300
BUILD_SECONDS = 600
CACHE_DAYS = 1
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _failed(reason: str) -> MarketError:
    return MarketError("market.env_failed", reason)


def _root(service: WorkspaceService) -> Path:
    return Path(os.path.realpath(service.state_dir)) / envtree.ADDON_ENV


def _ensure(service: WorkspaceService, *names: str) -> Path:
    """``<state>/addon-env[/<ws>[/...]]``, private to the host user (a directory an agent made first is refused)."""
    path = _root(service)
    ensure_private_dir(path, service.host_uid)
    for n in names:
        path = path / n
        ensure_private_dir(path, service.host_uid)
    return path


@contextlib.contextmanager
def _cache(service: WorkspaceService) -> Iterator[download.Cache]:
    d = _ensure(service, ".dl")
    fd = os.open(d, _DIR)
    try:
        yield download.Cache(fd, service.host_uid)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _flock(service: WorkspaceService, name: str, *, block: bool = True) -> Iterator[bool]:
    """The build lock of one ``(workspace, name)``. With ``block=False`` yields ``False`` when somebody holds it."""
    d = _ensure(service, service.workspace_id)
    fd = os.open(d / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            oslock.flock(fd, oslock.LOCK_EX | (0 if block else oslock.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------ stage time (D3)
def _wheel_paths(names: list[str]) -> list[str]:
    """The paths a wheel's members install to under ``pip --target``: ``*.data/{purelib,platlib,data}/x`` land at the
    root as ``x``; scripts and headers go elsewhere and do not matter here."""
    out = []
    for n in names:
        if n.endswith("/"):
            continue
        head, _, rest = n.partition("/")
        if head.endswith(".data"):
            scheme, _, tail = rest.partition("/")
            if scheme in ("purelib", "platlib", "data") and tail:
                out.append(tail)
        else:
            out.append(n)
    return out


_CD_SIG = b"PK\x01\x02"
_CD_FIXED = 46
_ZIP_ENTRY_MAX = _CD_FIXED + 3 * 65535  # the largest a central directory entry can be


def scan_zip(fd: int, budget: envtree.Budget) -> list[tuple[str, int]]:
    """``[(name, uncompressed size)]`` of a zip, read by walking its central directory OURSELVES, one entry at a time,
    drawing every entry and every declared byte from ``budget`` before the entry is kept. ``zipfile`` is never asked to
    list a file whose directory has not been walked here: an end record that lies about its count or size cannot make
    anything allocate more than the budget (the walk stops at the first entry past it). Zip64, a count that disagrees
    with what the directory holds, a name that is not UTF-8 or a directory that leaves the file: ``package``."""
    size = os.fstat(fd).st_size
    tail_at = max(size - (22 + 65535), 0)
    tail = os.pread(fd, size - tail_at, tail_at)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        raise TreeError("package")
    count, cd_size, cd_off = (
        struct.unpack("<H", tail[at + 10 : at + 12])[0],
        *struct.unpack("<LL", tail[at + 12 : at + 20]),
    )
    eocd = tail_at + at
    if (
        count == 0xFFFF
        or 0xFFFFFFFF in (cd_size, cd_off)
        or cd_off + cd_size != eocd  # the directory ends EXACTLY at the end record: no slack for a second directory
        or b"PK\x06\x07" in tail[max(at - 20, 0) : at]  # a zip64 locator before the end record
        or b"PK\x06\x06" in tail  # a zip64 end record anywhere near it
    ):
        raise TreeError("package")  # zip64, or a directory that zipfile (and so pip) could read differently
    out: list[tuple[str, int]] = []
    pos, end, buf = cd_off, cd_off + cd_size, b""
    while pos < end or buf:
        while len(buf) < _CD_FIXED and pos < end:
            chunk = os.pread(fd, min(1 << 16, end - pos), pos)
            if not chunk:
                raise TreeError("package")
            buf, pos = buf + chunk, pos + len(chunk)
        if len(buf) < _CD_FIXED or buf[:4] != _CD_SIG:
            raise TreeError("package")
        n, m, k = struct.unpack("<HHH", buf[28:34])
        need = _CD_FIXED + n + m + k
        while len(buf) < need and pos < end:
            chunk = os.pread(fd, min(1 << 16, end - pos, need - len(buf)), pos)
            if not chunk:
                raise TreeError("package")
            buf, pos = buf + chunk, pos + len(chunk)
        if len(buf) < need:
            raise TreeError("package")
        usize = struct.unpack("<L", buf[24:28])[0]
        if usize == 0xFFFFFFFF or len(out) >= count:  # zip64, or more entries than the end record says
            raise TreeError("package")
        budget.take(usize, 1)  # before the entry is kept
        try:
            out.append((buf[_CD_FIXED : _CD_FIXED + n].decode("utf-8"), usize))
        except UnicodeDecodeError:
            raise TreeError("package") from None
        buf = buf[need:]
    if len(out) != count:
        raise TreeError("package")
    _agree_with_zipfile(fd, out)
    return out


def _agree_with_zipfile(fd: int, ours: list[tuple[str, int]]) -> None:
    """The invariant (see :class:`envtree.Budget`): what orch judged is exactly what pip will write. pip lists the wheel
    with the standard library's ``zipfile``; so do we, now that our own walk has bounded what it can hold, and the two
    lists must be identical. Any parser differential, known or not, is refused here."""
    try:
        with os.fdopen(os.dup(fd), "rb") as f, zipfile.ZipFile(f) as z:
            theirs = [(i.filename, i.file_size) for i in z.infolist()]
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError):
        raise TreeError("package") from None
    finally:
        os.lseek(fd, 0, os.SEEK_SET)  # the dup shares the offset: the caller copies and hashes from the start
    if theirs != ours:
        raise TreeError("package")


def _judge_wheel(fd: int, budget: envtree.Budget) -> None:
    """The wheel's own member list against the environment path rules (a wheel that would put a ``.pth`` or a
    ``sitecustomize`` at the root, or a path outside it, is refused before the person signs). The members and the bytes
    they declare (what pip will write) are drawn from the shared ``budget`` of this environment."""
    try:
        names = scan_zip(fd, budget)
        envtree.check_paths(_wheel_paths([n for n, _ in names]))
    except OSError:
        raise _failed("package") from None
    except TreeError as e:
        raise _failed(e.reason) from None


def stage_fetch(service: WorkspaceService, lock: lockfile.Lock, planned: Any) -> dict[int, tuple[int, bool]]:
    """Download and verify every artifact this computer would install, at ``market.stage`` time (D3): the person signs
    knowing the package list, its size and which wheels are compiled, and a missing or wrong artifact refuses the
    stage while the old version still runs. Nothing is unpacked or installed. Returns ``{row index: (bytes,
    compiled)}``. Runs off the worker."""
    deadline = time.monotonic() + STAGE_SECONDS
    budget = download.new_budget()
    parse = envtree.Budget()  # every wheel and tarball of this stage draws from this one
    found: dict[int, tuple[int, bool]] = {}
    with _cache(service) as cache:
        for i, p in enumerate(planned):
            if not p.install:
                continue
            src = download.locate(p.row, lock.kind, deadline)
            fd = cache.fetch(src, deadline, budget)
            try:
                compiled = src.compiled
                if lock.kind == "node-package-lock":
                    try:
                        compiled = untar.unpack(fd, None, parse).compiled  # judged now, written only at build time
                    except TreeError as e:
                        raise _failed(e.reason) from None
                else:
                    _judge_wheel(fd, parse)
                found[i] = (os.fstat(fd).st_size, compiled)
            finally:
                os.close(fd)
    return found


# ------------------------------------------------------------------------------------------------ build
def snapshot(service: WorkspaceService, name: str) -> dict[str, Any]:
    """On the worker, under the store lock: the latest grant of ``name`` that is on and has ``runtime``, else
    ``market.not_installed``. The lock is released before anything slow."""
    store = service.store
    with store.locked():
        store.refresh()
        model = install.as_addons(store.state.workspace.addons).get(name)
        if model is None or not model.enabled or model.purged or model.runtime is None:
            raise MarketError("market.not_installed")
        return {"digest": model.package_sha256, "runtime": {k: model.runtime[k] for k in ("kind", "lock")}}


def build(service: WorkspaceService, name: str, grant: dict[str, Any], root: str) -> dict[str, Any]:
    """Steps 2 to 7 (see the module docstring), for the grant read by :func:`snapshot`. Off the worker; step 6 hops
    onto it. Returns ``{name, state, count, bytes}``."""
    try:
        pkg, manifest = install.read_installed(root, name)
    except (PackageError, ManifestError):
        raise _failed("package") from None
    rt = manifest.runtime
    if pkg.digest != grant["digest"] or rt is None or {k: rt[k] for k in ("kind", "lock")} != grant["runtime"]:
        raise _failed("package")  # the folder is not the bytes of the grant
    lock = install.read_lock(manifest, pkg)  # the only source of lock bytes: this in-memory package
    planned = lockfile.plan(lock, lockfile.current_host())
    deadline = time.monotonic() + BUILD_SECONDS
    base = _ensure(service)
    _ensure(service, service.workspace_id)
    used: list[str] = []
    with _flock(service, name):
        build_dir = Path(tempfile.mkdtemp(prefix=".build-", dir=base))
        try:
            env = build_dir / "env"
            env.mkdir(0o755)
            with _cache(service) as cache:
                if lock.kind == "python-requirements":
                    _build_python(build_dir, cache, lock, planned, deadline, used)
                else:
                    _build_node(env, cache, lock, planned, deadline, used)
            try:
                _normalize(env)
                tree = envtree.walk(env)
            except TreeError as e:
                raise _failed(e.reason) from None
            return service.submit(_install, service, name, grant, build_dir, env, tree, lock.kind, used).result()
        finally:
            _rm_name(base, build_dir.name)


def _build_python(
    build_dir: Path, cache: download.Cache, lock: lockfile.Lock, planned: Any, deadline: float, used: list[str]
) -> None:
    wheels = build_dir / "wheels"
    wheels.mkdir(0o700)
    lines = []
    budget = download.new_budget()
    parse = envtree.Budget()  # every wheel of this environment draws from this one
    for p in planned:
        if not p.install:
            continue
        src = download.locate(p.row, lock.kind, deadline)
        fd = cache.fetch(src, deadline, budget)
        try:
            _judge_wheel(fd, parse)  # the member list and declared sizes, again, before pip writes anything
            _copy_verified(fd, wheels / src.filename, src)
        finally:
            os.close(fd)
        used.append(f"{src.algo}-{src.digest}")
        lines.append(f"{p.row.name}=={p.row.version} --hash=sha256:{src.digest}\n")
    (build_dir / "req.txt").write_text("".join(lines))
    (build_dir / "home").mkdir(0o700)
    (build_dir / "tmp").mkdir(0o700)
    argv = [
        sys.executable, "-I", "-S", "-c", _FSIZE_TRAMPOLINE, str(untar.MAX_FILE), sys.executable, "-I", "-m", "pip",
        "install", "--isolated", "--no-index", "--find-links", str(wheels),
        "--require-hashes", "--no-deps", "--only-binary=:all:", "--no-compile", "--no-cache-dir", "--no-input",
        "--disable-pip-version-check", "--target", str(build_dir / "env"), "-r", str(build_dir / "req.txt"),
    ]  # fmt: skip
    # the installer's environment is made from nothing: no PIP_*, no proxy, no PYTHON*, no per-user config
    child = {
        "PATH": "/usr/bin:/bin", "HOME": str(build_dir / "home"), "TMPDIR": str(build_dir / "tmp"), "LANG": "C.UTF-8",
        "PIP_CONFIG_FILE": os.devnull,  # --isolated skips the user config; this skips the global and site files too
    }  # fmt: skip
    log = build_dir / "pip.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen(  # noqa: S603  (argv is built here from checked rows, no shell)
            argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=child, cwd=build_dir / "tmp",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
        end = time.monotonic() + PIP_SECONDS
        while True:
            try:
                code = proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if (
                    time.monotonic() > end
                    or os.fstat(out.fileno()).st_size > PIP_LOG_MAX
                    or _tree_size(build_dir / "env", envtree.MAX_BYTES) > envtree.MAX_BYTES
                ):  # the disk is never filled by an installer that is merely slow
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(proc.pid, 9)
                    proc.wait()
                    raise _failed("installer") from None
    if code != 0:
        with open(log, "rb") as f:  # only the tail is read, whatever the size
            f.seek(max(os.fstat(f.fileno()).st_size - 1500, 0))
            tail = f.read(1500).decode("utf-8", "replace")
        LOG.warning("addon env: pip failed (%s): %s", code, tail)
        raise _failed("installer")


#: sets the file size limit (the largest file pip may write) and replaces itself with pip: no ``preexec_fn`` in a
#: threaded host (the runner's trampoline, same shape)
_FSIZE_TRAMPOLINE = """
import os, resource, sys
n = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_FSIZE, (n, n))
os.execv(sys.argv[2], sys.argv[2:])
"""


def _tree_size(path: Path, limit: int) -> int:
    """The bytes below ``path`` (links not followed), stopping as soon as ``limit`` is passed."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                        if total > limit:
                            return total
        except OSError:
            continue
    return total


def _copy_verified(fd: int, dest: Path, src: download.Source) -> None:
    """Copy the cached artifact into the build directory, hashing again as it goes (pip sees what was verified)."""
    import hashlib

    h = hashlib.new(src.algo)
    out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(out, "wb") as f:
            while chunk := os.read(fd, 1 << 16):
                h.update(chunk)
                f.write(chunk)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    if h.hexdigest() != src.digest:
        raise _failed("hash_mismatch")


def _mkdir_p(root_fd: int, parts: list[str]) -> int:
    fd = os.dup(root_fd)
    for part in parts:
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, 0o755, dir_fd=fd)
        nxt = os.open(part, _DIR, dir_fd=fd)
        os.close(fd)
        fd = nxt
    return fd


def _build_node(
    env: Path, cache: download.Cache, lock: lockfile.Lock, planned: Any, deadline: float, used: list[str]
) -> None:
    budget = download.new_budget()
    parse = envtree.Budget()  # every tarball of this environment draws from this one
    root = os.open(env, _DIR)
    try:
        for p in planned:
            if not p.install:
                continue
            src = download.locate(p.row, lock.kind, deadline)
            fd = cache.fetch(src, deadline, budget)
            try:
                dest = _mkdir_p(root, p.row.key.split("/"))  # the lock key is checked: node_modules/<name>[/...]
                try:
                    untar.unpack(fd, dest, parse)
                except TreeError as e:
                    raise _failed(e.reason) from None
                finally:
                    os.close(dest)
            finally:
                os.close(fd)
            used.append(f"{src.algo}-{src.digest}")
    finally:
        os.close(root)


def _normalize(env: Path) -> None:
    """Modes of the environment: files 0644 (0755 when any execute bit was set), directories 0755."""
    for dirpath, dirnames, filenames in os.walk(env, followlinks=False):
        os.chmod(dirpath, 0o755)
        for n in dirnames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
        for n in filenames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                continue  # the walk refuses it next
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fchmod(fd, 0o755 if os.fstat(fd).st_mode & 0o111 else 0o644)
            finally:
                os.close(fd)


def _rm_name(base: Path, name: str) -> None:
    fd = os.open(base, _DIR)
    try:
        envtree.rmtree_at(fd, name)
    finally:
        os.close(fd)


def _install(
    service: WorkspaceService, name: str, grant: dict[str, Any], build_dir: Path, env: Path, tree: Any, kind: str,
    used: list[str],
) -> dict[str, Any]:  # fmt: skip
    """Steps 6 and 7 on the worker: the grant is read again under the store lock; a changed grant discards the build;
    the target is never adopted; the stamp is written last."""
    store = service.store
    ws = service.workspace_id
    digest = grant["digest"]
    with store.locked():
        store.refresh()
        model = install.as_addons(store.state.workspace.addons).get(name)
        if (
            model is None or not model.enabled or model.purged or model.package_sha256 != digest
            or model.runtime is None or {k: model.runtime[k] for k in ("kind", "lock")} != grant["runtime"]
        ):  # fmt: skip
            raise _failed("superseded")
        ndir = _ensure(service, ws, name)  # the flock of this (workspace, name) is held by :func:`build`
        nfd = os.open(ndir, _DIR)
        try:
            short = digest.removeprefix("sha256:")[:16]
            envtree.rmtree_at(nfd, short)  # whatever is there was not built by this run: removed, never adopted
            with contextlib.suppress(OSError):
                os.unlink(short + ".stamp.json", dir_fd=nfd)
            os.rename(env, short, dst_dir_fd=nfd)
            body = envtree.stamp_body(ws, name, digest, kind, tree)
            write_atomic(ndir / (short + ".stamp.json"), json.dumps(body, sort_keys=True).encode(), mode=0o600)
            for other in sorted(os.listdir(nfd)):  # step 7: every other digest of this name goes
                if other != short and other != short + ".stamp.json":
                    envtree.rmtree_at(nfd, other)
        finally:
            os.close(nfd)
    _drop_cache(service, used)
    return {"name": name, "state": "ready", "count": len(tree.entries), "bytes": tree.bytes}


def _drop_cache(service: WorkspaceService, used: list[str]) -> None:
    with contextlib.suppress(Exception), _cache(service) as cache:
        for n in used:
            cache.drop(n)


# ------------------------------------------------------------------------------------------------ read, remove, recover
def env_row(service: WorkspaceService, name: str, model: Any) -> dict[str, Any] | None:
    """The ``env`` of a ``market.installed`` row: ``None`` for an add-on without ``runtime``, else ``{state, kind,
    count, bytes, built_at, reason}`` with ``state`` one of ``ready``, ``needs_setup``, ``changed``, ``building``."""
    if model is None or model.runtime is None:
        return None
    kind = model.runtime["kind"]
    row: dict[str, Any] = {"state": "ready", "kind": kind, "count": 0, "bytes": 0, "built_at": None, "reason": None}
    try:
        with _flock(service, name, block=False) as free:
            if not free:
                return {**row, "state": "building"}
    except (OSError, Exception):  # noqa: BLE001 - an unreadable lock directory is "needs setup", never a crash
        return {**row, "state": "needs_setup", "reason": "missing"}
    try:
        envtree.verify(str(os.path.realpath(service.state_dir)), service.workspace_id, name, model.package_sha256, kind)
    except MarketError as e:
        return {**row, "state": "needs_setup" if e.reason == "missing" else "changed", "reason": e.reason}
    state = os.path.realpath(service.state_dir)
    target = envtree.target_dir(state, service.workspace_id, name, model.package_sha256)
    stamp = envtree.read_stamp(envtree.stamp_path(target), service.host_uid)
    if stamp is not None:
        row.update(count=stamp["files"], bytes=stamp["bytes"], built_at=stamp["built_at"])
    return row


def remove(service: WorkspaceService, name: str) -> None:
    """``market.uninstall``: the add-on's whole environment directory goes (by descriptor, links never followed)."""
    d = _root(service) / service.workspace_id
    if not d.is_dir() or d.is_symlink():
        return
    fd = os.open(d, _DIR)
    try:
        envtree.rmtree_at(fd, name)
        with contextlib.suppress(OSError):
            os.unlink(f"{name}.lock", dir_fd=fd)
    finally:
        os.close(fd)


def recover(service: WorkspaceService) -> None:
    """At start, after :func:`orch.host.marketinstall.recover`: leftovers of a crashed build are removed. A build that
    never reached its rename leaves only ``.build-*``; one that crashed after the rename but before the stamp leaves an
    unstamped target (never adopted: removed here, rebuilt by the next ``market.env_build``)."""
    root = _root(service)
    if not root.is_dir() or root.is_symlink():
        return
    try:
        fd = os.open(root, _DIR)
    except OSError:
        return
    try:
        for n in os.listdir(fd):
            if n.startswith(".build-"):
                envtree.rmtree_at(fd, n)
        _recover_ws(service, fd)
        if ".dl" in os.listdir(fd):
            _recover_cache(fd)
    except Exception:  # noqa: BLE001 - recovery never stops the host from starting
        LOG.exception("addon env: recovery failed")
    finally:
        os.close(fd)


def _recover_ws(service: WorkspaceService, root_fd: int) -> None:
    ws = service.workspace_id
    if ws not in os.listdir(root_fd):
        return
    wfd = os.open(ws, _DIR, dir_fd=root_fd)
    try:
        from . import marketinstall

        installed = marketinstall.files_of(service).installs()[ADDONS]
        for name in os.listdir(wfd):
            if name.endswith(".lock"):
                continue
            model = install.as_addons(service.store.state.workspace.addons).get(name)
            if name not in installed or model is None or model.purged or model.runtime is None:
                envtree.rmtree_at(wfd, name)
                continue
            nfd = os.open(name, _DIR, dir_fd=wfd)
            try:
                keep = model.package_sha256.removeprefix("sha256:")[:16]
                for entry in os.listdir(nfd):
                    if entry in (keep, keep + ".stamp.json"):
                        continue
                    envtree.rmtree_at(nfd, entry)
                if keep in os.listdir(nfd) and not _stamped(nfd, keep, service.host_uid):
                    envtree.rmtree_at(nfd, keep)  # unstamped: built by nobody we know
            finally:
                os.close(nfd)
    finally:
        os.close(wfd)


def _stamped(nfd: int, short: str, uid: int) -> bool:
    return envtree.read_stamp(short + ".stamp.json", uid, nfd) is not None


def _recover_cache(root_fd: int) -> None:
    cfd = os.open(".dl", _DIR, dir_fd=root_fd)
    try:
        cutoff = time.time() - CACHE_DAYS * 86400
        for n in os.listdir(cfd):
            try:
                st = os.stat(n, dir_fd=cfd, follow_symlinks=False)
            except OSError:
                continue
            if n.startswith(".part-") or st.st_mtime < cutoff or not stat.S_ISREG(st.st_mode):
                envtree.rmtree_at(cfd, n)
    finally:
        os.close(cfd)
