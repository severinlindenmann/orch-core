"""The host's audit hook (D-P2-9, section 3): defence in depth, **not a sandbox**.

The host calls :func:`warm` (imports everything it will need), then :func:`install` once, at start (never in direct
mode). ``sys.addaudithook`` hooks cannot be removed, so tests install it in a subprocess.

What it does
------------
* **Spawns.** ``subprocess.Popen``, ``os.posix_spawn``, ``os.spawn*``, ``os.exec*``, ``os.system``, ``os.fork*`` and
  ``pty.spawn`` raise, except one call: ``subprocess.Popen`` or ``os.posix_spawn`` made by the **code object** of
  :func:`orch.host.launcher.spawn_gitread` (captured at install, compared by identity, and required to be the nearest
  frame that is not ``subprocess`` itself), with ``executable``/path equal to ``sys.executable``, argv equal to
  :func:`orch.host.launcher.gitread_argv`, ``env == {}`` and ``cwd is None``.
* **Opens.** ``open`` outside the roots raises. The event carries a path string, so this check is lexical and not
  race-free: the real boundary is :class:`orch.ops.caps.Files` (fd walk, ``O_NOFOLLOW``). The hook reads a path the
  way ``Files`` does: absolute, a component-wise prefix of a root, no ``..``/``.``/empty component, no NUL, no
  normalisation. A relative name is allowed only as a single component opened with ``O_NOFOLLOW`` (the ``dir_fd``
  shape ``Files`` uses) **and** only while the process cwd is inside a root: :func:`install` changes the cwd to the
  first root, and ``os.chdir`` is itself checked.
* **Mutations and listings.** The same root check applies to ``os.rename``, ``os.remove``, ``os.rmdir``, ``os.mkdir``,
  ``os.link``, ``os.symlink`` (the link, not its text), ``os.truncate``, ``os.chmod``, ``os.chown``, ``os.utime``,
  ``os.listdir``, ``os.scandir``, ``os.chdir`` and the ``shutil`` copy/move/remove events. Reads of metadata
  (``stat``) are not gated.
* **ctypes.** ``ctypes.dlopen`` and ``ctypes.dlsym`` raise after install: preload every library first.
* **Person custody (H5c, orch-v2-host §0.8.6).** The host never handles a person's key: an open (or any path event) of
  a path under ``<root>/person`` or with the components ``person/<workspace id>/devices`` raises, ``Security.framework``
  is never loaded, and :data:`NEVER` (the click signer and the convenience custody modules) is neither warmed nor
  importable after install. In tier A this is an implementation invariant, not an OS boundary.

Known limits (say so; do not rely on the hook for more)
-------------------------------------------------------
* ``_posixsubprocess.fork_exec`` emits no audit event and cannot be blocked from here; neither can code that already
  holds a loaded libc handle (``ctypes.CDLL(None)`` made before install). The real spawn boundary is the OS: a
  separate UID, rlimits and (H3b1) the reader child's own limits.
* Imports and package data are opens too. Anything not loaded before ``install`` fails closed: that is what
  :func:`warm` is for.
* File descriptors (``open(3)``) pass: they name an object already opened. ``/dev/null`` passes (the launcher's
  stderr); ``/dev/tty`` is outside every root and refused.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import re
import sys
from collections.abc import Iterable
from typing import Any

__all__ = ["install", "make_hook", "warm"]

_SPAWN_EVENTS = {
    "subprocess.Popen",
    "os.posix_spawn",
    "os.spawn",
    "os.exec",
    "os.system",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
}
_LAUNCHER_EVENTS = {"subprocess.Popen", "os.posix_spawn"}
_NO_LOAD = {"ctypes.dlopen", "ctypes.dlsym"}
#: modules the host never imports (H5c): the click signer and person custody that signs without a factor
NEVER = (
    "orch.click",
    "orch.custody.pkvault",
    "orch.custody.convenience",
    "orch.custody.keychain",
    "orch.custody.select",
    "orch.custody.cli",
)


def _never(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in NEVER)


_WS = re.compile(r"[0-9a-f]{32}")
#: file names of person custody, refused as relative names whatever directory fd they are opened from (a ``dir_fd``
#: walk cannot be resolved from the audit event): convenience keys, the selection, the click log, the H5b passphrase
#: device key
_PERSON_NAMES = re.compile(r"d_[0-9A-Za-z_]{1,64}\.key|custody\.json|click\.jsonl(\.[0-9])?|dk\.key\.json")
#: event -> indices of its path arguments (a link's text and a dir_fd are not paths)
_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.chdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.link": (0, 1),
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.mkdir": (0,),
    "os.mkfifo": (0,),
    "os.mknod": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "shutil.chown": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
}


#: Standard-library modules that are imported lazily (error paths, codecs, the database) and would otherwise be
#: first loaded after the hook.
_STDLIB = (
    "encodings.idna",
    "encodings.ascii",
    "encodings.latin_1",
    "unicodedata",
    "hashlib",
    "json",
    "sqlite3",
    "traceback",
    "linecache",
    "tokenize",
    "_colorize",
    "difflib",
    "fcntl",
    "termios",
    "ctypes",
    "tempfile",
    "uuid",
    "_strptime",
    "zoneinfo",
    "locale",
    "calendar",
    "shlex",
    "zlib",
)


_THIRD_PARTY = ("cryptography", "jsonschema", "referencing", "jsonschema_specifications", "rpds")


#: subpackages of third-party trees that are never imported by :func:`warm`: test suites and benchmarks
_SKIP_PARTS = frozenset({"tests", "benchmarks"})


def _skipped(module: str) -> bool:
    return any(part in _SKIP_PARTS for part in module.split(".")[1:])


def _import_tree(name: str) -> None:
    try:
        pkg = importlib.import_module(name)
    except ImportError:
        return
    # pkgutil.walk_packages would import every subpackage to recurse into it, test suites included; this walk never
    # imports a skipped package (a package's own tests pull in pytest or hypothesis: H3a note c)
    todo = [(getattr(pkg, "__path__", []), name + ".")]
    while todo:
        path, prefix = todo.pop()
        for info in pkgutil.iter_modules(path, prefix):
            if info.name.endswith(".__main__") or _skipped(info.name):  # __main__: importing a script runs it
                continue
            try:
                module = importlib.import_module(info.name)
            except Exception:  # an optional backend that is not installed: nothing to preload
                continue
            if info.ispkg:
                todo.append((getattr(module, "__path__", []), info.name + "."))


def warm() -> None:
    """Import every ``orch`` module, build the registry and load the package data, so that nothing is loaded lazily
    after :func:`install` (a late import opens files and fails closed). The host calls this before ``install``."""
    import orch

    todo = [(list(orch.__path__), "orch.")]  # not walk_packages: it would import a skipped package to recurse
    while todo:
        path, prefix = todo.pop()
        for info in pkgutil.iter_modules(path, prefix):
            if info.name.endswith(".__main__") or _never(info.name):
                continue
            module = importlib.import_module(info.name)
            if info.ispkg:
                todo.append((list(getattr(module, "__path__", [])), info.name + "."))
    for dep in _THIRD_PARTY:  # their lazy imports (backends, validators) would open files after the hook
        _import_tree(dep)
    import orch.ops as ops

    ops.get("status")  # loads the registry
    from orch.instructions import builtin_skills

    builtin_skills()  # reads the skill files once (cached)
    from orch import schema

    schema.preload()
    for mod in _STDLIB:
        try:
            importlib.import_module(mod)
        except ImportError:  # a module this Python does not have (for example _colorize before 3.13)
            pass


def _under(parts: list[str], roots: list[tuple[str, ...]]) -> bool:
    return any(tuple(parts[: len(r)]) == r for r in roots)


def _lexical(path: str) -> list[str] | None:
    """The components of an absolute path as ``Files`` reads it, or ``None`` if ``Files`` would refuse it."""
    if "\0" in path or not path.startswith("/"):
        return None
    parts = path.split("/")[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(c in ("", ".", "..") for c in parts):
        return None
    return parts


def _argv_of(args: tuple) -> list[str] | None:
    # subprocess.Popen: (executable, args, cwd, env); os.posix_spawn: (path, argv, env)
    argv = args[1] if len(args) > 1 else None
    try:
        return [os.fsdecode(a) for a in argv] if argv is not None else None
    except TypeError:
        return None


def make_hook(roots: Iterable[str | os.PathLike[str]]):
    from orch.host import launcher

    root_parts = [tuple(c for c in os.fspath(r).split("/") if c) for r in roots]
    allowed_argv = launcher.gitread_argv()
    launcher_code = launcher.spawn_gitread.__code__
    exe = sys.executable
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def launcher_call(event: str, args: tuple) -> bool:
        if _argv_of(args) != allowed_argv:
            return False
        if event == "subprocess.Popen":
            executable, _argv, cwd, env = args
            ok = executable in (None, exe) and cwd is None and env == {}
        else:  # os.posix_spawn: (path, argv, env)
            ok = args[0] == exe and args[2] == {}
        if not ok:
            return False
        f: Any = sys._getframe(2)  # 0 is this function, 1 the hook, 2 whoever raised the event
        while f is not None and f.f_globals.get("__name__") == "subprocess":
            f = f.f_back
        return f is not None and f.f_code is launcher_code

    def cwd_inside() -> bool:
        try:
            parts = _lexical(os.getcwd())
        except OSError:
            return False
        return parts is not None and _under(parts, root_parts) and not person_key(parts)

    person_parts = [(*r, "person") for r in root_parts]

    def person_key(parts: list[str]) -> bool:
        if _under(parts, person_parts):
            return True
        if parts and _PERSON_NAMES.fullmatch(parts[-1]):
            return True
        for i in range(len(parts) - 2):
            if not _WS.fullmatch(parts[i + 1]):
                continue
            if parts[i] == "person" and parts[i + 2] == "devices":
                return True  # <person state>/person/<ws>/devices/…
            if parts[i] == "hosts" and parts[i + 2] == "person":
                return True  # <state>/hosts/<ws>/person/… (H5b's passphrase device key)
        return False

    def allowed(p: Any, open_flags: int | None = None) -> bool:
        if p is None or isinstance(p, int):
            return True
        try:
            s = os.fsdecode(p)
        except TypeError:
            return False
        if s == os.devnull and open_flags is not None:  # subprocess.DEVNULL (the launcher's stderr)
            return True
        if s.startswith("/"):
            parts = _lexical(s)
            return parts is not None and _under(parts, root_parts) and not person_key(parts)
        # relative: one component, never a link (for open), and only while the cwd is inside a root
        if "/" in s or "\0" in s or s in ("", "..") or _PERSON_NAMES.fullmatch(s):
            return False
        if open_flags is not None and not open_flags & nofollow:
            return False
        return cwd_inside()

    def hook(event: str, args: tuple) -> None:
        if event in _SPAWN_EVENTS:
            if event in _LAUNCHER_EVENTS and launcher_call(event, args):
                return
            raise PermissionError(f"host audit: {event} is not allowed")
        if event == "import" and args and isinstance(args[0], str) and _never(args[0]):
            raise PermissionError(f"host audit: the host never imports {args[0]}")
        if event in _NO_LOAD:
            if args and "Security.framework" in str(args[0]):
                raise PermissionError("host audit: the host never loads Security.framework (person custody)")
            raise PermissionError(f"host audit: {event} is not allowed after start")
        if event == "open":
            path, _mode, flags = args
            if not allowed(path, flags if flags is not None else 0):
                raise PermissionError(f"host audit: open outside the roots: {path!r}")
            return
        idx = _PATH_EVENTS.get(event)
        if idx is not None:
            for i in idx:
                if i < len(args) and not allowed(args[i]):
                    raise PermissionError(f"host audit: {event} outside the roots: {args[i]!r}")

    return hook


def install(roots: Iterable[str | os.PathLike[str]]) -> None:
    """Install the hook for this process, for good, after :func:`warm`. ``roots``: the workspace root and the state
    directory, by their real paths. Changes the working directory to the first root: a relative name is then inside
    the roots."""
    roots = list(roots)
    hook = make_hook(roots)  # imports the launcher before the hook exists
    os.chdir(roots[0])
    sys.addaudithook(hook)
