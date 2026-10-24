"""The ``dev-pair`` sandbox files, shared by production code and ``dev-pair`` (orch-v2-host §11A.17, 11A.15.11).

**Accident prevention, not containment.** Same-UID code can forge every file here. What keeps a real workspace safe is
its signed genesis: only a workspace whose genesis has the reserved prefix ``SBX`` and the owner name
``orch dev sandbox <id8>`` is ever a sandbox workspace (§11A.17.3, S3), and production ``orch setup`` gives ``SBX`` only
to a fresh throwaway person.

The files (all 0600 regular files of this UID, opened with ``O_NOFOLLOW`` through a pinned directory descriptor):

* ``<root>/.orch-dev-sandbox``, the **manifest** (``dev-pair up`` writes it first with neither ``person_id`` nor
  ``workspaces``: a **pending** dev host starts from that, admits one fresh ``SBX`` bootstrap by ``orch setup``, and
  is stopped; the kit then adds ``person_id`` and ``workspaces`` and starts the full dev host (V3-D38))::

      {"id": "<8 or more lowercase hex>", "root": {"st_dev": int, "st_ino": int},
       "person_id": "p_<32 hex>",
       "workspaces": [{"id": "<32 hex>", "st_dev": int, "st_ino": int}]}

  ``root`` is the identity of ``<root>`` itself; each ``workspaces`` entry the identity of the workspace root
  (``<root>/ws`` or ``<root>/ws-<n>``). Other keys are allowed and ignored.
* ``<root>/state/host/dev.json``, the **sandbox host config**, read only by a host started with
  ``--dev-sim-convenience``::

      {"v": 1, "sim_convenience": true, "sandbox_id": "<manifest id>",
       "root": {"st_dev": int, "st_ino": int}, "relay_origin": "<scheme://host[:port]>"}

  Exactly these keys.
* ``<ws>/.state/dev-sandbox.json``, the **workspace marker** (only narrows: on a normal host it refuses)::

      {"v": 1, "sandbox_id": "<manifest id>", "workspace_id": "<32 hex>"}

The writers at the end (:func:`write_manifest`, :func:`write_dev_json`, :func:`write_marker`) are the ones ``dev-pair``
and the tests use; production code never calls them.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from orch import layout

__all__ = [
    "DEV_JSON",
    "MANIFEST",
    "OWNER_PREFIX",
    "RESERVED_PREFIX",
    "SANDBOX_ID",
    "WS_MARKER",
    "SandboxRefused",
    "check_manifest",
    "click_refusal",
    "dev_state",
    "find_sandbox_root",
    "manifest_at",
    "marker_present",
    "open_root",
    "owner_name",
    "parse_dev_json",
    "read_marker",
    "read_private",
    "write_dev_json",
    "write_manifest",
    "write_marker",
]

MANIFEST = ".orch-dev-sandbox"
DEV_JSON = "dev.json"
WS_MARKER = "dev-sandbox.json"
RESERVED_PREFIX = "SBX"
OWNER_PREFIX = "orch dev sandbox "
#: the manifest id: 8 or more lowercase hex characters (§11A.17.3, D4)
SANDBOX_ID = re.compile(r"[0-9a-f]{8,128}")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_WS = re.compile(r"[0-9a-f]{32}")
_MAX = 65536
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class SandboxRefused(Exception):  # noqa: N818 - a verdict, as orch.custody's errors
    """A sandbox file is missing, not private, malformed or names another root."""

    code = "sandbox.refused"


def owner_name(sandbox_id: str) -> str:
    """The exact owner name of a sandbox workspace's genesis: ``orch dev sandbox <first 8 characters of the id>``."""
    return OWNER_PREFIX + sandbox_id[:8]


def _pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping) or set(value) - {"st_dev", "st_ino"}:
        return None
    dev, ino = value.get("st_dev"), value.get("st_ino")
    if type(dev) is not int or type(ino) is not int:
        return None
    return dev, ino


def open_root(root: str | os.PathLike[str]) -> tuple[int, os.stat_result]:
    """A directory descriptor of ``root`` (no link followed), which must be a private directory of this UID; the
    caller closes it."""
    try:
        st = os.lstat(root)
    except OSError:
        raise SandboxRefused("no sandbox root") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SandboxRefused("the sandbox root is not a private directory of this user")
    try:
        fd = os.open(root, _DIR)
    except OSError:
        raise SandboxRefused("the sandbox root cannot be opened") from None
    got = os.fstat(fd)
    if (got.st_dev, got.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise SandboxRefused("the sandbox root changed while it was opened")
    return fd, got


def read_private(dir_fd: int, name: str) -> bytes:
    """A 0600 regular file of this UID beneath ``dir_fd``, opened without following a link and without blocking (a
    FIFO in its place never hangs the open)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except OSError:
        raise SandboxRefused(f"no {name}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise SandboxRefused(f"{name} is not a 0600 file of this user")
        buf = b""
        while len(buf) <= _MAX:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            buf += chunk
        if len(buf) > _MAX:
            raise SandboxRefused(f"{name} is too large")
        return buf
    finally:
        os.close(fd)


def _json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(raw)
    except ValueError:
        raise SandboxRefused(f"the {what} is not JSON") from None
    if not isinstance(doc, dict):
        raise SandboxRefused(f"the {what} is not an object")
    return doc


def manifest_at(dir_fd: int, st: os.stat_result) -> dict[str, Any]:
    """The manifest beneath the pinned root ``dir_fd`` (whose ``fstat`` is ``st``), checked: ``id`` of 8 or more
    lowercase hex characters, ``root`` equal to the root's identity, ``person_id`` (when present) a person id,
    ``workspaces`` (when present) a list of ``{id, st_dev, st_ino}``."""
    doc = _json(read_private(dir_fd, MANIFEST), "sandbox manifest")
    sid = doc.get("id")
    if type(sid) is not str or not SANDBOX_ID.fullmatch(sid):
        raise SandboxRefused("the sandbox manifest id is not 8 or more lowercase hex characters")
    if _pair(doc.get("root")) != (st.st_dev, st.st_ino):
        raise SandboxRefused("the sandbox manifest does not name this root")
    if "person_id" in doc and (type(doc["person_id"]) is not str or not _PERSON.fullmatch(doc["person_id"])):
        raise SandboxRefused("the sandbox manifest's person_id is not a person id")
    rows = doc.get("workspaces", [])
    if not isinstance(rows, list) or len(rows) > 64:
        raise SandboxRefused("the sandbox manifest's workspaces is not a list")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "st_dev", "st_ino"}
            or type(row["id"]) is not str
            or not _WS.fullmatch(row["id"])
            or _pair({"st_dev": row["st_dev"], "st_ino": row["st_ino"]}) is None
        ):
            raise SandboxRefused("a sandbox manifest workspaces entry is malformed")
    return doc


def check_manifest(root: str | os.PathLike[str]) -> dict[str, Any]:
    """The manifest of ``root`` (:func:`manifest_at` through a fresh descriptor), or :class:`SandboxRefused`."""
    fd, st = open_root(root)
    try:
        return manifest_at(fd, st)
    finally:
        os.close(fd)


def parse_dev_json(raw: bytes) -> dict[str, Any]:
    """``dev.json``, exactly its five keys and the optional ``market`` (the origin's syntax is checked by the host)."""
    doc = _json(raw, "sandbox host config")
    base = {"v", "sim_convenience", "sandbox_id", "root", "relay_origin"}
    if not base <= set(doc) <= base | {"market"}:
        raise SandboxRefused("dev.json has unexpected keys")
    if "market" in doc:
        _check_market(doc["market"])
    if doc["v"] != 1 or doc["sim_convenience"] is not True:
        raise SandboxRefused("dev.json is not v1 with sim_convenience true")
    if type(doc["sandbox_id"]) is not str or not SANDBOX_ID.fullmatch(doc["sandbox_id"]):
        raise SandboxRefused("dev.json's sandbox_id is not a sandbox id")
    if _pair(doc["root"]) is None:
        raise SandboxRefused("dev.json's root is not {st_dev, st_ino}")
    if type(doc["relay_origin"]) is not str or not 1 <= len(doc["relay_origin"]) <= 512:
        raise SandboxRefused("dev.json's relay_origin is not a string")
    return doc


def _check_market(m: Any) -> None:
    """``market: {url, keys}``: the sandbox's test marketplace (V3 G3, C5): a canonical https URL and one to three
    publisher/index keys (65-byte uncompressed P-256 points, unpadded base64url). Nothing else."""
    from orch.crypto.encoding import EncodingError, unb64u
    from orch.market import MarketError
    from orch.market.url import canonical_market_url

    if not isinstance(m, dict) or set(m) != {"url", "keys"}:
        raise SandboxRefused("dev.json's market is not {url, keys}")
    try:
        canonical_market_url(m["url"])
    except MarketError:
        raise SandboxRefused("dev.json's market url is not a canonical marketplace URL") from None
    keys = m["keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 3 or len(set(map(str, keys))) != len(keys):
        raise SandboxRefused("dev.json's market keys are one to three distinct keys")
    for k in keys:
        try:
            unb64u(k, 65)
        except (EncodingError, TypeError, ValueError):
            raise SandboxRefused("dev.json's market key is not a P-256 public key") from None


def find_sandbox_root(path: str | os.PathLike[str]) -> Path | None:
    """The nearest ancestor of ``path`` (itself included, as written and resolved) that holds a manifest entry of any
    kind, or ``None``. A presence test: it only ever narrows."""
    seen: list[Path] = []
    with contextlib.suppress(OSError, ValueError):
        seen.append(Path(os.path.abspath(path)))
    with contextlib.suppress(OSError, ValueError):
        seen.append(Path(os.path.realpath(path)))
    for start in seen:
        for ancestor in (start, *start.parents):
            with contextlib.suppress(OSError, ValueError):
                if os.path.lexists(ancestor / MANIFEST):
                    return ancestor
    return None


def dev_state(state_dir: str | os.PathLike[str]) -> bool:
    """``state_dir`` is a sandbox's state: ``<state>/host/dev.json`` exists and parses, ``<state>/..`` holds a valid
    manifest with the same id and root, and ``state`` is ``<root>/state`` by identity. Any doubt is ``False`` here;
    callers that refuse use it together with :func:`find_sandbox_root` (only narrowing)."""
    state = Path(os.path.abspath(state_dir))
    try:
        raw = (state / "host" / DEV_JSON).read_bytes() if os.path.lexists(state / "host" / DEV_JSON) else None
    except OSError:
        return True  # a dev.json that exists but cannot be read: refuse rather than guess
    if raw is None:
        return False
    try:
        fd, st = open_root(state.parent)
    except SandboxRefused:
        return False
    try:
        manifest = manifest_at(fd, st)
        dev = parse_dev_json(raw)
        here = os.stat("state", dir_fd=fd, follow_symlinks=False)
        mine = os.lstat(state)
    except (SandboxRefused, OSError):
        return False
    finally:
        os.close(fd)
    return (
        dev["sandbox_id"] == manifest["id"]
        and _pair(dev["root"]) == (st.st_dev, st.st_ino)
        and (here.st_dev, here.st_ino) == (mine.st_dev, mine.st_ino)
    )


#: set by a host process whose dev start-up proof passed (orch.host.devmode): the click signer is refused in it
_PROCESS_DEV_HOST = False


def mark_dev_process() -> None:
    global _PROCESS_DEV_HOST
    _PROCESS_DEV_HOST = True


def click_refusal(state_dir: str | os.PathLike[str], env: Mapping[str, str]) -> str | None:
    """``"dev_host"`` when the click signer (``orch host convenience on``, ``select.convert_to_convenience``,
    ``orch keys custody convenience``) must refuse (§11A.17.2, F3, D2): in a dev host process, against a sandbox's
    state (:func:`dev_state`, independent of the environment), or with ``HOME`` under a sandbox root. Else ``None``."""
    if _PROCESS_DEV_HOST or dev_state(state_dir):
        return "dev_host"
    home = env.get("HOME")
    if home and find_sandbox_root(home) is not None:
        return "dev_host"
    return None


def marker_present(ws_root: str | os.PathLike[str]) -> bool:
    """``<store>/.state/dev-sandbox.json`` exists in any form (a narrowing signal: a broken one counts too). Fails
    closed, in both candidate places: see :func:`orch.layout.state_marker_present`."""
    return layout.state_marker_present(ws_root, WS_MARKER)


def read_marker(ws_root: str | os.PathLike[str]) -> dict[str, Any]:
    """The workspace marker, checked (0600, this UID, no link, ``{v:1, sandbox_id, workspace_id}``)."""
    try:
        fd = layout.open_state_fd(ws_root)
    except layout.LayoutError as exc:
        raise SandboxRefused(str(exc)) from None
    if fd is None:
        raise SandboxRefused("no workspace .state directory")
    try:
        doc = _json(read_private(fd, WS_MARKER), "workspace marker")
    finally:
        os.close(fd)
    if (
        set(doc) != {"v", "sandbox_id", "workspace_id"}
        or doc["v"] != 1
        or type(doc["sandbox_id"]) is not str
        or type(doc["workspace_id"]) is not str
    ):
        raise SandboxRefused("the workspace marker is malformed")
    return doc


# -- writers: dev-pair and the tests (never production code) ----------------------------------------------------------


def _write(path: Path, doc: Any) -> None:
    """Atomic: a fresh 0600 temporary file beside ``path`` (``O_EXCL``, no link followed), then ``rename``."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(doc, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_at(dir_fd: int, name: str, doc: Any) -> None:
    """:func:`_write` below a directory fd."""
    tmp = f".{name}.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.write(fd, json.dumps(doc, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _ident(path: str | os.PathLike[str]) -> dict[str, int]:
    st = os.lstat(path)
    return {"st_dev": st.st_dev, "st_ino": st.st_ino}


def write_manifest(
    root: str | os.PathLike[str],
    sandbox_id: str,
    *,
    person_id: str | None = None,
    workspaces: Iterable[tuple[str, str | os.PathLike[str]]] = (),
) -> dict[str, Any]:
    """Write (or replace) ``<root>/.orch-dev-sandbox`` with ``root``'s identity and, for each ``(workspace id, root)``,
    that workspace root's identity."""
    doc: dict[str, Any] = {"id": sandbox_id, "root": _ident(root)}
    if person_id is not None:
        doc["person_id"] = person_id
    rows = [{"id": wid, **_ident(ws)} for wid, ws in workspaces]
    if rows:
        doc["workspaces"] = rows
    _write(Path(root) / MANIFEST, doc)
    return doc


def write_dev_json(
    root: str | os.PathLike[str], sandbox_id: str, relay_origin: str, market: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Write ``<root>/state/host/dev.json`` (the directories must exist); ``market`` is the optional
    test marketplace of the sandbox (C5)."""
    doc = {
        "v": 1,
        "sim_convenience": True,
        "sandbox_id": sandbox_id,
        "root": _ident(root),
        "relay_origin": relay_origin,
    }
    if market is not None:
        _check_market(market)
        doc["market"] = market
    _write(Path(root) / "state" / "host" / DEV_JSON, doc)
    return doc


def write_marker(ws_root: str | os.PathLike[str], sandbox_id: str, workspace_id: str) -> dict[str, Any]:
    """Write ``<store>/.state/dev-sandbox.json`` (``.state`` must exist), below the pinned ``.state`` fd."""
    doc = {"v": 1, "sandbox_id": sandbox_id, "workspace_id": workspace_id}
    try:
        fd = layout.open_state_fd(ws_root)
    except layout.LayoutError as exc:
        raise SandboxRefused(str(exc)) from None
    if fd is None:
        raise SandboxRefused("no workspace .state directory")
    try:
        _write_at(fd, WS_MARKER, doc)
    finally:
        os.close(fd)
    return doc
