"""The company bundles on the host (orch-hub ``contracts/hub/protocol.md`` 7.3 and 7.4, M3 #1430 #1435).

``verify_org_skills`` and ``verify_org_mcp`` check a ``hub.org_skills`` / ``hub.mcp_servers`` bundle the way
:func:`orch.hubclient.snapshot.verify_snapshot` checks a snapshot: signed by ``HK_op`` under a card the host already
verified, naming this hub and this workspace, a ``version`` that never goes down. The items are checked with the host's
own copy of the text and URL rules (:mod:`orch.hubclient.orgtext`) and a digest that is recomputed, never trusted.

A verified bundle only OFFERS items: nothing here enables one. The owner's accept (a later slice) decides,
per digest."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from . import wire
from .errors import HubRefused
from .orgtext import mcp_problem, skill_problem

__all__ = [
    "MCP_MAX",
    "SECRETS_MAX",
    "SIG_MCP_SERVERS",
    "SIG_ORG_SECRETS",
    "SIG_ORG_SKILLS",
    "SKILLS_MAX",
    "mcp_digest",
    "skill_digest",
    "secrets_digest",
    "valid_org_mcp",
    "valid_org_secrets",
    "valid_org_skills",
    "verify_org_mcp",
    "verify_org_secrets",
    "verify_org_skills",
]

SIG_ORG_SKILLS = b"orch/v2/hub-org-skills|"
SIG_MCP_SERVERS = b"orch/v2/hub-mcp-servers|"
SIG_ORG_SECRETS = b"orch/v2/hub-org-secrets|"  # the hub's label name is sig_hub_org_secrets
SKILLS_MAX = 20
MCP_MAX = 20
SECRETS_MAX = 50
_SEALED_MIN = 65 + 8 + 16  # eph_pub || shortest value || tag
_SEALED_MAX = 65 + 4096 + 16
_DIGEST_PREFIX = "sha256:"


def skill_digest(name: str, text: str) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(wire.cj({"name": name, "text": text})).hexdigest()


def mcp_digest(s: Mapping[str, Any]) -> str:
    return (
        _DIGEST_PREFIX
        + hashlib.sha256(wire.cj({k: s[k] for k in ("name", "url", "auth", "secret", "description")})).hexdigest()
    )


def _is_digest(x: Any) -> bool:
    return (
        type(x) is str and x.startswith(_DIGEST_PREFIX) and len(x) == 71 and all(c in "0123456789abcdef" for c in x[7:])
    )


def valid_org_skills(x: Any) -> bool:
    """``skills``: ``{name, text, digest}`` entries sorted by unique name; text rules and digest rechecked."""
    if type(x) is not list or len(x) > SKILLS_MAX:
        return False
    names = []
    for s in x:
        if type(s) is not dict or set(s) != {"name", "text", "digest"}:
            return False
        if skill_problem(s["name"], s["text"]) is not None or not _is_digest(s["digest"]):
            return False
        if s["digest"] != skill_digest(s["name"], s["text"]):
            return False
        names.append(s["name"])
    return names == sorted(set(names))


def valid_org_mcp(x: Any) -> bool:
    """``servers``: ``{name, url, auth, secret, description, digest}`` entries, nothing else (no command field)."""
    if type(x) is not list or len(x) > MCP_MAX:
        return False
    names = []
    for s in x:
        if type(s) is not dict or set(s) != {"name", "url", "auth", "secret", "description", "digest"}:
            return False
        if mcp_problem(s) is not None or not _is_digest(s["digest"]) or s["digest"] != mcp_digest(s):
            return False
        names.append(s["name"])
    return names == sorted(set(names))


def valid_org_secrets(x: Any) -> bool:
    """``secrets``: ``{id, name, version, expires_ms, wxk_version, sealed}`` entries sorted by name. A repeated name or
    id makes the WHOLE bundle invalid (the host never guesses which one the hub meant). ``sealed`` is only shape
    checked here: the host cannot open it without the workspace key."""
    from orch import crypto
    from orch.crypto import suite

    from .orgsecrets import NAME

    if type(x) is not list or len(x) > SECRETS_MAX:
        return False
    names, ids = [], set()
    for s in x:
        if type(s) is not dict or set(s) != {"id", "name", "version", "expires_ms", "wxk_version", "sealed"}:
            return False
        if not wire.hex16(s["id"]) or type(s["name"]) is not str or NAME.fullmatch(s["name"]) is None:
            return False
        if not wire._int_min(1)(s["version"]) or s["version"] >= 2**32:  # noqa: SLF001
            return False
        if not wire._int_min(0)(s["expires_ms"]) or s["expires_ms"] >= 2**64:  # noqa: SLF001
            return False
        if not wire._int_min(1)(s["wxk_version"]) or s["wxk_version"] >= 2**32:  # noqa: SLF001
            return False
        try:
            raw = crypto.unb64u(s["sealed"])
            if not _SEALED_MIN <= len(raw) <= _SEALED_MAX:
                return False
            suite.validate_public_key(raw[:65])  # it must start with a real ephemeral point
        except Exception:  # noqa: BLE001
            return False
        if s["id"] in ids:
            return False
        ids.add(s["id"])
        names.append(s["name"])
    return names == sorted(set(names))


def secrets_digest(o: Mapping[str, Any]) -> str:
    """The pin of a verified secrets bundle: its canonical bytes, hashed (the wraps themselves are not all kept)."""
    return _DIGEST_PREFIX + hashlib.sha256(wire.cj(dict(o))).hexdigest()


def _spec(items: str, valid: Callable[[Any], bool]) -> dict[str, Callable[[Any], bool]]:
    return {
        "hub_id": wire.hex16,
        "op_cert_id": wire.hex16,
        "workspace_id": wire.hex16,
        "version": wire._int_min(1),  # noqa: SLF001
        "issued_ms": wire._int_min(0),  # noqa: SLF001
        items: valid,
        "dev": lambda x: type(x) is bool,
    }


_KINDS = {
    "hub.org_skills": (SIG_ORG_SKILLS, _spec("skills", valid_org_skills)),
    "hub.mcp_servers": (SIG_MCP_SERVERS, _spec("servers", valid_org_mcp)),
    "hub.org_secrets": (SIG_ORG_SECRETS, _spec("secrets", valid_org_secrets)),
}


def _verify(
    kind: str,
    signed: Any,
    held: Mapping[str, Any],
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str],
) -> dict[str, Any]:
    label, spec = _KINDS[kind]
    if held["op_cert"]["cert_id"] in revoked_op_certs:  # the CURRENT revoked set, at every use
        raise HubRefused("revoked")
    o = wire._envelope(signed, kind)  # noqa: SLF001 - the one envelope/signature implementation
    wire._signature(signed, label, held["hk_op_pub"])  # noqa: SLF001
    if set(o) != {"v", "suite", "kind", "hv", *spec} or not all(ok(o[k]) for k, ok in spec.items()):
        raise HubRefused("malformed")
    if o["hub_id"] != held["card"]["hub_id"]:
        raise HubRefused("other_hub")
    if o["op_cert_id"] != held["op_cert"]["cert_id"]:
        raise HubRefused("stale_op_cert")
    wire.check_dev(o["dev"], dev_mode)
    if o["workspace_id"] != workspace_id:
        raise HubRefused("other_workspace")
    if pinned is not None:
        if o["version"] < pinned["version"]:
            raise HubRefused("stale_bundle")
        if o["version"] == pinned["version"]:
            if wire.cj(o) != wire.cj(dict(pinned)):
                raise HubRefused("bundle_conflict")
            return dict(pinned)
    return o


def verify_org_skills(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """``held`` is the result of :func:`wire.verify_card`; ``pinned`` the ``o`` of the bundle the host holds. Returns
    the object to hold next (the pinned one for an identical re-send). Refusals: ``revoked``, ``malformed``,
    ``bad_signature``, ``other_hub``, ``stale_op_cert``, ``other_workspace``, ``stale_bundle``, ``bundle_conflict``,
    ``dev_refused``."""
    return _verify("hub.org_skills", signed, held, workspace_id, dev_mode, pinned, revoked_op_certs)


def verify_org_mcp(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The same rules for ``hub.mcp_servers`` (its own label and kind: a skills bundle is not a servers bundle)."""
    return _verify("hub.mcp_servers", signed, held, workspace_id, dev_mode, pinned, revoked_op_certs)


def verify_org_secrets(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    dev_mode: bool,
    pinned: Mapping[str, Any] | None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The same rules for ``hub.org_secrets`` (own label and kind). ``pinned`` is the held ``o`` (as for the other bundles) or ``{"version", "digest"}`` of the
    bundle the host last verified (see :func:`secrets_digest`; the bundle itself is not kept, only the wraps a served
    server uses). A lower version is ``stale_bundle``; the same version with other bytes is ``bundle_conflict``. A
    repeated secret name or id is ``malformed`` for the whole bundle."""
    o = _verify("hub.org_secrets", signed, held, workspace_id, dev_mode, None, revoked_op_certs)
    if pinned is not None:
        same = wire.cj(o) == wire.cj(dict(pinned)) if "secrets" in pinned else secrets_digest(o) == pinned["digest"]
        if o["version"] < pinned["version"]:
            raise HubRefused("stale_bundle")
        if o["version"] == pinned["version"] and not same:
            raise HubRefused("bundle_conflict")
    return o
