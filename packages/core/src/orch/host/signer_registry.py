"""Person-side workspace discovery shared by signing clients."""
from pathlib import Path
from typing import Any

from .errors import HostError


def _socket(run: Path | None, state: Path, host_uid: int, workspace_id: str) -> Path:
    """``--run``, else the tier-A ``<state>/run`` (H7: a path that merely exists under the tier-B run directory never
    wins)."""
    from .enable import run_dir_for

    return run_dir_for(state, run) / workspace_id / "host.sock"


def _own_workspaces(state: Path, uid: int, pick: str | None) -> list[dict[str, Any]]:
    """The workspaces of this person's own registry where this UID is mapped to a person (never workspace config)."""
    from .custody import read_json
    from .registry import registry_path

    doc = read_json(registry_path(state), uid) or {"workspaces": []}
    out = []
    for item in doc.get("workspaces") or []:
        peer = (item.get("peers") or {}).get(str(uid)) or {}
        if peer.get("class") != "person" or not peer.get("person"):
            continue
        if pick is not None and pick not in (item.get("workspace_id"), item.get("prefix")):
            continue
        out.append(item)
    return out



def _backend(state: Path, workspace_id: str) -> Any:
    """The device key's custody from the person's selection (H5c, orch-v2-host §0.8.6): the convenience backend when
    selected, else the passphrase key at ``<state>/hosts/<ws>/person/`` (H5b's location). The binding is the signer
    tuple, so a convenience selection binds ``auth: "convenience"`` and the host's readers condition applies."""
    from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
    from orch.custody.select import device_backend

    try:
        backend, _ = device_backend(state, workspace_id)
    except KeyNotFound:
        raise HostError("custody.no_key", "this machine holds no device key of yours for this workspace") from None
    except BackendUnavailable as exc:
        raise HostError("custody.unavailable", str(exc)[:400]) from None
    except CustodyError as exc:
        raise HostError("custody.unavailable", str(exc)[:400]) from None
    return backend


