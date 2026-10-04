"""Where addons are found (spec A1 §4). Reads manifests only; never imports addon code."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orch.addons.manifest import MANIFEST_NAME, Manifest, load_manifest
from orch.errors import OrchError

_PACKAGED = Path(__file__).resolve().parent.parent / "default_addons"  # wheels: hatch force-include of addons/
_SOURCE = Path(__file__).resolve().parents[3] / "addons"  # plugin root: source checkout and the bin/orch wrapper


def default_addons_dir() -> Path:
    return _PACKAGED if _PACKAGED.is_dir() else _SOURCE


def custom_addons_dir() -> Path:
    from orch.dashboard.launch import config_dir
    return config_dir() / "addons"


@dataclass(frozen=True)
class Found:
    name: str
    kind: str  # "default" | "custom"
    folder: Path
    manifest: Manifest | None
    error: str | None = None


def _scan(base: Path, kind: str) -> list[Found]:
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError:
        return []
    out = []
    for folder in folders:
        if not (folder / MANIFEST_NAME).is_file():
            continue
        try:
            m = load_manifest(folder)
        except OrchError as e:
            out.append(Found(folder.name, kind, folder, None, e.message))
            continue
        if m.name != folder.name:
            out.append(Found(folder.name, kind, folder, m,
                             f"folder {folder.name!r} does not match the manifest name {m.name!r}"))
            continue
        out.append(Found(m.name, kind, folder, m))
    return out


def discover() -> list[Found]:
    defaults = _scan(default_addons_dir(), "default")
    taken = {f.name for f in defaults}
    custom = [f if f.name not in taken else
              Found(f.name, f.kind, f.folder, f.manifest, f"ignored: {f.name!r} is the name of a default addon")
              for f in _scan(custom_addons_dir(), "custom")]
    return defaults + custom


def find(name: str) -> Found | None:
    """The addon called `name`; a default addon wins over a custom one of the same name."""
    return next((f for f in discover() if f.name == name), None)
