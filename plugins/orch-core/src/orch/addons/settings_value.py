"""The value of an add-on's settings (``settings.set {addons: {<name>: value}}``, design G6 section 3).

Two layers, in this order:

* :func:`check_shape` -- the general value checks. Replay, the host and the signer all run it, BEFORE any schema:
  jsonschema 2020-12 accepts ``1.0`` as an integer, so a float is refused here, and size and depth are bounded here,
  not by the add-on's own schema (review F1).
* :func:`check_granted` -- the host's check against the schema of the manifest that was GRANTED (the installed package
  must be that exact package). Run inside the store lock on the re-read state, at prepare and again at submit.

Both return ``None`` or a short reason; nothing here raises on hostile input.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

__all__ = ["KEY", "MAX_BYTES", "MAX_DEPTH", "check_changes", "check_granted", "check_shape", "leaves"]

#: the ``settings.set`` key (one name for the store-layout lint: a bare "addons" literal names a store directory)
KEY = "addons"
MAX_BYTES = 8 * 1024
MAX_DEPTH = 4  # nesting of containers, the value itself is depth 1
MAX_STRING = 200
MAX_ITEMS = 64
_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(?![\s\S])")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}(?![\s\S])")
_SAFE_INT = 2**53 - 1


def _text(s: str) -> bool:
    return len(s) <= MAX_STRING and not any(unicodedata.category(c)[0] == "C" or c in "  " for c in s)


def _walk(v: Any, depth: int) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return None if -_SAFE_INT <= v <= _SAFE_INT else "a number is out of range"
    if isinstance(v, float):
        return "no decimal numbers"
    if isinstance(v, str):
        return None if _text(v) else f"a text is at most {MAX_STRING} printable characters"
    if depth > MAX_DEPTH:
        return f"nested deeper than {MAX_DEPTH}"
    if isinstance(v, Mapping):
        if len(v) > MAX_ITEMS:
            return f"more than {MAX_ITEMS} entries"
        for k, sub in v.items():
            if not isinstance(k, str) or not _KEY.fullmatch(k):
                return "a key is lower-case letters, digits . _ -"
            if (why := _walk(sub, depth + 1)) is not None:
                return why
        return None
    if isinstance(v, list | tuple):
        if len(v) > MAX_ITEMS:
            return f"more than {MAX_ITEMS} entries"
        for sub in v:
            if (why := _walk(sub, depth + 1)) is not None:
                return why
        return None
    return "a value that is not plain data"


def check_shape(value: Any) -> str | None:
    """``None`` when ``value`` is ``null`` (removes) or an object within the general bounds, else the reason."""
    from orch import canon

    if value is None:
        return None
    if not isinstance(value, Mapping):
        return "the settings of an add-on are an object"
    if (why := _walk(value, 1)) is not None:
        return why
    try:
        if len(canon.dumps(value)) > MAX_BYTES:
            return f"more than {MAX_BYTES // 1024} KiB"
    except Exception:  # noqa: BLE001 - anything canon refuses (a float, a lone surrogate) is a refusal
        return "not plain data"
    return None


def check_changes(addons: Any) -> str | None:
    """The shape of ``set.addons``: exactly one add-on name, whose value passes :func:`check_shape`. Replay and the
    signer use this alone; the host adds :func:`check_granted`."""
    if not isinstance(addons, Mapping) or len(addons) != 1:
        return "one add-on at a time"
    ((name, value),) = addons.items()
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return "not an add-on name"
    return check_shape(value)


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, Mapping):
        for k, sub in v.items():
            yield k
            yield from _strings(sub)
    elif isinstance(v, list | tuple):
        for sub in v:
            yield from _strings(sub)


def check_granted(store: Any, addons: Any) -> str | None:
    """The host's check on the re-read state of ``store`` (call it inside ``store.locked()``): the shape, then the
    add-on is granted, enabled, not purged, the installed package is exactly the granted one, its manifest has
    ``settings``, no string looks like a secret, and the value validates against the manifest's schema (``null``
    removes and passes)."""
    if (why := check_changes(addons)) is not None:
        return why
    ((name, value),) = addons.items()
    from orch.addons import install
    from orch.addons.manifest import ManifestError
    from orch.addons.package import PackageError

    found = install.as_addons(store.state.workspace.addons).get(name)
    if found is None or not found.enabled or found.purged:
        return "that add-on is not on"
    try:
        pkg, mf = install.read_installed(store.layout.store, name)
    except (PackageError, ManifestError, OSError):
        return "that add-on cannot be read here"
    if pkg.digest != found.package_sha256:
        return "the installed package is not the one that was turned on"
    spec = mf.doc.get("settings")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("schema"), Mapping):
        return "that add-on has no settings"
    if value is None:
        return None
    from orch.tokenshape import GITHUB_TOKEN_SHAPE

    if any(GITHUB_TOKEN_SHAPE.search(s) for s in _strings(value)):
        return "a value looks like an access key: keys are stored with the add-on's own login, not in settings"
    from jsonschema import Draft202012Validator

    if next(Draft202012Validator(dict(spec["schema"])).iter_errors(value), None) is not None:
        return "the settings do not match what that add-on declares"
    return None


def leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every leaf of ``value`` as ``(path, leaf)``, in key order; an empty object or list is itself a leaf."""
    if isinstance(value, Mapping) and value:
        return [x for k in sorted(value) for x in leaves(value[k], f"{path}.{k}" if path else k)]
    if isinstance(value, list | tuple) and value:
        return [x for i, v in enumerate(value) for x in leaves(v, f"{path}[{i}]")]
    return [(path, value)]
