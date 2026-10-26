"""Turning on an add-on that needs settings, as a sequence (the ``addon_enable`` intent; design of 19 Oct 2026).

ONE review and ONE confirmation sign, in this order, on the WORKSPACE log:

0. ``settings.set {<setting>: value}`` -> ``settings.changed`` (the address of Publish);
1. ``addon.grant {name}`` -> ``addon.granted`` (the add-on is on) -- always LAST.

Every prefix is safe: after step 0 the add-on is "set up, not on". Both payloads are built from the intent alone
(AE1); the signer compares what the host prepared with them byte for byte (:func:`check_step`). The grant is the
``addon.granted`` body the dashboard read with ``addon.preview``; the host recomputes it at step 0
(:func:`host_check_first`) and the signer compares the prepared event at step 1 with it (AE5).

The rules about which add-ons need which setting live in :mod:`orch.addons.settings_needs` (AE4).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from orch import canon

__all__ = [
    "KIND",
    "check_step",
    "host_check_first",
    "lines",
    "payload",
    "steps",
    "validate",
]

KIND = "addon_enable"
_KEYS = {"v", "kind", "name", "settings", "grant"}
_GRANT_KEYS = {"name", "version", "package_sha256", "capabilities", "fields", "sections", "artifact_kinds"}


def validate(intent: Any, bad: type[Exception]) -> dict[str, Any]:
    """The canonical intent or ``bad``: exactly ``{v, kind, name, settings, grant}``, fail closed."""
    from orch.addons.settings_needs import NEEDS_SETTINGS

    if not isinstance(intent, Mapping) or set(intent) != _KEYS:
        raise bad("turning on an add-on has exactly v, kind, name, settings and grant")
    if intent["v"] != 1 or intent["kind"] != KIND:
        raise bad("this signer does not know that sequence")
    name = intent["name"]
    if not isinstance(name, str) or name not in NEEDS_SETTINGS:
        raise bad("this add-on is turned on without settings: use the plain grant")
    key = NEEDS_SETTINGS[name]
    settings = intent["settings"]
    if not isinstance(settings, Mapping) or set(settings) != {key}:
        raise bad(f"the settings of {name} are exactly {key}")
    value = _setting(name, key, settings[key], bad)
    return {"v": 1, "kind": KIND, "name": name, "settings": {key: value}, "grant": _grant(name, intent["grant"], bad)}


def _setting(name: str, key: str, value: Any, bad: type[Exception]) -> Any:
    from jsonschema import Draft202012Validator

    from orch.ops import person_ops

    if value is None:
        raise bad("clearing a setting is not turning an add-on on")
    if next(Draft202012Validator(person_ops.SET_SCHEMA).iter_errors({key: value}), None) is not None:
        raise bad(f"the {key} setting is not valid")
    return copy.deepcopy(value)


def _grant(name: str, grant: Any, bad: type[Exception]) -> dict[str, Any]:
    from orch import schema

    if not isinstance(grant, Mapping) or set(grant) != _GRANT_KEYS or grant["name"] != name:
        raise bad("the grant is the addon.granted body of this add-on")
    sig = "A" * 86
    actor = {"kind": "person", "id": "p_" + "0" * 32, "device": "d_" + "0" * 32}
    head = "sha256:" + "0" * 64
    probe = {
        "v": 2, "id": "0" * 26, "seq": 2, "at": "2026-01-01T00:00:00Z", "prev": head, "based_on": head, "hash_v": 1,
        "roster_v": 1, "host_sig": sig, "sig": sig, "actor": actor, "auth": "passphrase",
        "type": "addon.granted", **copy.deepcopy(dict(grant)),
    }  # fmt: skip
    try:
        schema.validate("event.addon.granted", probe, log="workspace")
    except Exception as exc:  # noqa: BLE001 - any refusal of the schema is a refusal of the intent
        raise bad(f"the grant is not an addon.granted event ({str(exc)[:80]})") from None
    return copy.deepcopy(dict(grant))


def steps(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Both steps are person ops on the workspace log (``log``): the settings first, the grant last."""
    return [
        {"op": "settings.set", "args": {"set": copy.deepcopy(intent["settings"])}, "kind": "settings", "ref": False,
         "log": "workspace"},
        {"op": "addon.grant", "args": {"name": intent["name"]}, "kind": "addon", "ref": False, "log": "workspace"},
    ]  # fmt: skip


def payload(intent: Mapping[str, Any], k: int) -> dict[str, Any]:
    """The payload (type and payload keys, no envelope) of the event of step ``k``, from the intent alone (AE1)."""
    if k == 0:
        return {"type": "settings.changed", "set": copy.deepcopy(intent["settings"])}
    return {"type": "addon.granted", **copy.deepcopy(intent["grant"])}


def check_step(intent: Mapping[str, Any], k: int, prepared: Mapping[str, Any], stop: Callable[[], Exception]) -> None:
    """Refuse (``stop()``) unless the request the host prepared for step ``k`` is, byte for byte, the event the reviewed
    intent says, on the workspace log: no ticket, no texts, and (step 1) no sequence or ``before`` material. The chain
    of step 1 (the workspace tail this signer's step 0 left) is the host's check, re-checked inside its lock at submit
    (AE6), as for the runner grant of the quick task."""
    from .sequence import _ENVELOPE, _EVENTS

    plan = steps(intent)
    events = prepared.get(_EVENTS)
    material = prepared.get("material")
    if (
        not 0 <= k < len(plan)
        or prepared.get("op") != plan[k]["op"]
        or not isinstance(events, list)
        or len(events) != 1
        or not isinstance(events[0], Mapping)
        or not isinstance(material, Mapping)
        or prepared.get("log") != "workspace"
        or material.get("ticket") is not None
        or material.get("kind") != "payload"
        or material.get("texts")
    ):
        raise stop()
    before = material.get("before")
    if k == 0:
        # the host's current values of the keys this step sets (unsigned, labelled so in the review): nothing else
        if before is not None and (not isinstance(before, Mapping) or not set(before) <= set(intent["settings"])):
            raise stop()
    elif "sequence" in material or before:
        raise stop()
    got = {key: v for key, v in events[0].items() if key not in _ENVELOPE}
    if canon.dumps(got) != canon.dumps(payload(intent, k)):
        raise stop()


def host_check_first(
    store: Any, person: str, intent: Mapping[str, Any], *, dry_run: bool, err: Callable[[str, str], Exception]
) -> None:
    """The host's checks at step 0, before anything is signed (``err(code, message)`` builds its error): the person is a
    workspace owner (both ops are owner-only: refused now, not after step 0 landed), the add-on is not on already, and
    (not for a dry run) the package is still the one the person reviewed (AE5, AE7)."""
    from orch.addons import settings_needs as needs
    from orch.addons.manifest import ManifestError
    from orch.addons.package import PackageError

    name = intent["name"]
    member = store.state.workspace.members.get(person)
    if member is None or member.role != "owner":
        raise err("role.denied", "turning an add-on on is for workspace owners")
    if needs.is_on(store.state.workspace.addons, name):
        raise err("invalid.input", f"{needs.TITLES[name]} is already on; change its settings instead")
    if dry_run:
        return
    try:
        now = needs.preview_grant(store.layout.store, name)
    except (KeyError, PackageError, ManifestError):
        raise err("invalid.input", f"{needs.TITLES[name]} cannot be read here; nothing was signed") from None
    if canon.dumps(now) != canon.dumps(intent["grant"]):
        raise err("human.request_mismatch", "the add-on package changed, review again")


def lines(intent: Mapping[str, Any], clean: Callable[[Any], str]) -> list[str]:
    """The intent in plain words, for a terminal prompt that has no SignContent."""
    from orch.addons.settings_needs import TITLES
    from orch.host.signcontent import one

    g = intent["grant"]
    out = [f"turn on: {clean(one(TITLES[intent['name']]))}"]
    for key, value in intent["settings"].items():
        out.append(f"setting {clean(key)}: {clean(canon.dumps(value).decode('utf-8'))}")
    out.append(f"add-on version: {clean(one(g['version']))}")
    out.append(f"package: {clean(one(g['package_sha256']))}")
    out.append(f"capabilities: {clean(', '.join(g['capabilities']) or 'none')}")
    return out
