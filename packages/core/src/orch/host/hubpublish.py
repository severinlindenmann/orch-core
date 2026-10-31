"""What the host knows about WHERE ITS HUB says to publish (#1838, hub protocol 10.9.1), for the Publish add-on.

Three things, all read here and nowhere else:

* **The held object** (:func:`held`): the hub's signed ``publish_where`` of the hub this workspace is bound to now,
  checked again against the card pinned now on every call (:mod:`orch.hubclient.publish_held`). An object that no longer
  verifies is deleted from ``hub.json`` and reads as absent. It is the ONLY source of the Publish form's suggestion.
* **Provenance** (:func:`from_hub`): when the person signed the publish settings (the turn-on sequence or
  ``orch publish setup``) with values equal to the held object, the host records which hub they came from. The
  settings themselves are signed and keep their shape (``{server, share}``); the record is an unsigned local file,
  ``<state>/hosts/<workspace>/publish/from_hub.json`` (0600, next to ``registered.json``), bound to the exact values:
  ``{hub_id, hub_name, server, share}``. It is judged against the settings in force, so it never outlives them, and
  it is written or cleared when a ``settings.changed`` naming ``publish`` lands in the log (:func:`note_publish_set`):
  signing new publish settings with anything but the held values, or while no hub holds them, clears it.
* **The two states that follow** (:func:`left`, :func:`moved`): settings that came from hub X while the workspace is not
  bound to X are inert (``hub_left``), and settings that came from hub X whose held values moved are paused
  (``hub_moved``). Both are closed reasons of ``publish.server_refused``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["bound_hub_id", "clear", "from_hub", "held", "left", "moved", "note_publish_set", "status_fields"]

_LOG = logging.getLogger("orch.host.hubpublish")
_FILE = "from_hub.json"


def bound_hub_id(service: WorkspaceService) -> str | None:
    """The hub this workspace is joined to NOW by its signed log (``None``: standalone, or its newest hub record is a
    leave). A workspace that joined another hub since is bound to that one."""
    model = dict(service.store.state.workspace.hub)
    if model.get("status") != "joined":
        return None
    hub_id = model.get("hub_id")
    return hub_id if isinstance(hub_id, str) else None


def held(service: WorkspaceService) -> dict[str, Any] | None:
    """``{hub_id, hub_name, server, share, seq}`` of the hub this workspace is bound to, from its held signed object
    verified now; ``None`` when there is none or it no longer verifies (then it is deleted). Worker thread."""
    from orch.hubclient import publish_held

    from . import hubops

    try:
        with hubops._MU:  # noqa: SLF001
            state = hubops._load_state(service)  # noqa: SLF001
            if state is None or state.status != "bound" or state.hub_id != bound_hub_id(service):
                return None
            o, changed = publish_held.read(state, int(service.clock() * 1000))
            if changed:
                hubops._write_state(service, state)  # noqa: SLF001
    except HostError:
        return None  # an unreadable hub.json is "nothing held", never a guess
    if o is None:
        return None
    return {
        "hub_id": state.hub_id,
        "hub_name": state.hub_name,
        "server": o["server"],
        "share": o["share"],
        "seq": o["seq"],
    }


# -- provenance ----------------------------------------------------------------------------------------------------


def _path(service: WorkspaceService) -> Any:
    from . import publishing

    return publishing._base(service) / _FILE  # noqa: SLF001


def _settings(service: WorkspaceService) -> dict[str, Any] | None:
    from orch.store.render import thaw

    cfg = thaw(service.store.state.workspace.settings.get("publish"))
    if isinstance(cfg, dict) and isinstance(cfg.get("server"), str) and isinstance(cfg.get("share"), str):
        return cfg
    return None


def from_hub(service: WorkspaceService) -> dict[str, Any] | None:
    """``{hub_id, hub_name, server, share}`` when the publish settings in force came from a hub (see the module text),
    else ``None``. The record counts only while it names exactly the values in force."""
    from . import publishing

    try:
        doc = publishing._read_json(service, _path(service))  # noqa: SLF001
    except HostError:
        return None
    cfg = _settings(service)
    if (
        type(doc) is not dict
        or set(doc) != {"hub_id", "hub_name", "server", "share"}
        or type(doc["hub_id"]) is not str
        or not (doc["hub_name"] is None or type(doc["hub_name"]) is str)
        or cfg is None
        or (doc["server"], doc["share"]) != (cfg["server"], cfg["share"])
    ):
        return None
    return doc  # type: ignore[no-any-return]


def clear(service: WorkspaceService) -> None:
    import contextlib
    import os

    from . import publishing

    with publishing._MU, contextlib.suppress(OSError):  # noqa: SLF001
        os.unlink(_path(service))


def _same(value: str, canonical: str, dev: bool) -> bool:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.publish_where import canonical_origin

    try:
        return canonical_origin(value, dev) == canonical
    except HubRefused:
        return False


def note_publish_set(service: WorkspaceService, value: Any) -> None:
    """A ``settings.changed`` naming ``publish`` landed with ``value`` (``None``: removed). Record the provenance when
    the values equal what the bound hub's held object says, else clear it: signing publish settings again is the
    person's own decision and ends the hub's claim on them."""
    from . import hubops, publishing

    now = held(service)
    if (
        now is None
        or not isinstance(value, dict)
        or not isinstance(value.get("server"), str)
        or not isinstance(value.get("share"), str)
    ):
        clear(service)
        return
    dev = hubops.workspace_dev(service)
    if not (_same(value["server"], now["server"], dev) and _same(value["share"], now["share"], dev)):
        clear(service)
        return
    with publishing._MU:  # noqa: SLF001
        publishing._private(service)  # noqa: SLF001
        publishing._put_json(  # noqa: SLF001
            _path(service),
            {"hub_id": now["hub_id"], "hub_name": now["hub_name"], "server": value["server"], "share": value["share"]},
        )


def note_events(service: WorkspaceService, log: str, events: list[dict[str, Any]]) -> None:
    """After events landed in the log: apply :func:`note_publish_set` for each that is a ``settings.changed`` naming
    ``publish`` and is in the log. Best effort: provenance never stops an event from landing."""
    if log != "workspace":
        return
    for ev in events:
        sets = ev.get("set") if ev.get("type") == "settings.changed" else None
        if not isinstance(sets, dict) or "publish" not in sets or service.store.logged_event(log, ev["id"]) is None:
            continue
        try:
            note_publish_set(service, sets["publish"])
        except Exception:  # noqa: BLE001
            _LOG.warning("publish provenance not updated", exc_info=True)


# -- the two states ------------------------------------------------------------------------------------------------


def left(service: WorkspaceService) -> dict[str, Any] | None:
    """The provenance record when the settings in force came from hub X and this workspace is not bound to X now
    (it left, or it is bound elsewhere): they are inert. ``None`` otherwise."""
    prov = from_hub(service)
    if prov is None or bound_hub_id(service) == prov["hub_id"]:
        return None
    return prov


def moved(service: WorkspaceService) -> dict[str, str] | None:
    """``{server, share}`` (the new values) when the settings in force came from the hub this workspace is bound to and
    its verified held object now says other values; else ``None``."""
    prov = from_hub(service)
    if prov is None:
        return None
    now = held(service)
    if now is None or now["hub_id"] != prov["hub_id"]:
        return None
    if (now["server"], now["share"]) == (prov["server"], prov["share"]):
        return None
    return {"server": now["server"], "share": now["share"]}


def hub_left_error(prov: dict[str, Any]) -> HostError:
    name = prov.get("hub_name")
    return HostError(
        "publish.server_refused",
        f"Sharing went through your hub{f' {name}' if name else ''}. Connect again, or set a sharing server.",
        detail={"reason": "hub_left"},
    )


def hub_moved_error(new: dict[str, str]) -> HostError:
    host = urlsplit(new["share"]).netloc or new["share"]
    return HostError(
        "publish.server_refused",
        f"Sharing paused: your hub now shares through {host}.",
        detail={"reason": "hub_moved"},
    )


def status_fields(service: WorkspaceService) -> dict[str, Any]:
    """What ``hub.status`` carries for the Publish form: ``publish_where`` (the verified held values and the hub's name,
    or ``None``) and ``publish_from`` / ``publish_moved`` (see :func:`from_hub`, :func:`moved`)."""
    now = held(service)
    prov = from_hub(service)
    return {
        "publish_where": None
        if now is None
        else {"hub_id": now["hub_id"], "hub_name": now["hub_name"], "server": now["server"], "share": now["share"]},
        "publish_from": None if prov is None else {"hub_id": prov["hub_id"], "hub_name": prov["hub_name"]},
        "publish_moved": moved(service),
    }
