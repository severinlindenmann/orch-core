"""The install checks of spec 5, as pure functions (V3 G3; design ``state/design-g3.md``). No network, no disk, no
clock: the host gathers the facts (the cached index, the install record, the class) into a :class:`Ctx`, and these
functions give the answer ``tests/addons/market_ref.install`` gives on the same case (a test runs both).

* :func:`check_entry`: steps 6 to 9, on the entry of the index (revoked, host, reserved id, origin, version, rollback,
  publisher and TOFU, ``min_orch``). The hub ceiling is the policy hook's (``orch.market.policy``).
* :func:`check_package`: steps 13 and 14, 16, on the bytes of a package that was read once: digest, manifest agreement,
  compatibility, and step 15 (the lockfile named by the manifest's ``runtime``, V3 G4).
* :func:`diff`: what an update changes, and whether it ``widened`` what the add-on may do."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orch import canon
from orch.addons import builtin
from orch.addons.install import read_lock
from orch.addons.lockfile import Planned
from orch.addons.manifest import Manifest, ManifestError, _core_key, declarations, incompatibility, load_manifest
from orch.addons.package import MANIFEST_FILE, Package

from . import ADDONS, MarketError, deps, index, policy
from .url import host_of

__all__ = ["Ctx", "Facts", "check_entry", "check_package", "diff", "key_change_ok", "semver"]

ROTATED = "publisher key changed: a key this add-on has not used before (official keys rotate with orch releases)"


def semver(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def key_change_ok(cls: str, keys: Sequence[str], known: str | None, new: str | None) -> bool:
    """Whether an entry signed by ``new`` may follow the remembered key ``known`` (#1191). No record, or the same key:
    yes. A change (or a dropped key): only for the official class, and only to a key this core release pins
    (``keys`` = ``index.official_keys()`` read now). The old key need not still be pinned: a first install already
    accepts any pinned key, and a record left on a retired key must not block the next update. A retired key itself
    is never accepted (it is not in ``keys``); every other class keeps strict TOFU (``trust --replace``)."""
    if known is None or new == known:
        return True
    return cls == "official" and new is not None and new in keys


@dataclass(frozen=True)
class Ctx:
    """Everything step 6 to 9 read besides the index. ``cls`` is ``official`` or ``dev`` (the sandbox's test
    marketplace, C5): both need every entry to carry a publisher whose key is in ``keys``; ``dev`` has the reserved-id
    rule of a non-official market, except that it may list the official ids."""

    url: str
    cls: str
    keys: tuple[str, ...]
    core_version: str
    git_hosts: frozenset[str]
    installed: Mapping[str, Mapping[str, Any]]  # name -> install record {origin, version, package_sha256, ...}
    known_keys: Mapping[str, str]  # "<url>|<id>" -> publisher key
    seen: Mapping[str, str]  # "<url>|<id>" -> highest version seen
    builtin: frozenset[str] = frozenset(builtin.NAMES)
    official_ids: frozenset[str] = frozenset()
    moved: frozenset[str] = frozenset()
    hub: Any = None  # the hub's marketplace policy; ``None`` = standalone (D79)


@dataclass(frozen=True)
class Facts:
    entry: dict[str, Any]
    tofu: str  # "unsigned" | "new" (also an official key rotation) | "known" | "pinned" (a company key the hub pins)
    publisher_key: str | None
    prior: Mapping[str, Any] | None  # the install record of this name, if installed


def check_entry(ctx: Ctx, doc: Mapping[str, Any], addon_id: str) -> Facts:
    """Steps 6 to 9 of spec 5 in the order of the reference. The first failing check is the answer."""
    entry = next((e for e in doc[ADDONS] if e["id"] == addon_id), None)
    if entry is None:
        raise MarketError("market.unknown_addon")
    if f"{entry['id']}@{entry['version']}" in doc.get("revoked", []):
        raise MarketError("market.revoked")
    entry_host = host_of(entry["source"]["git"])
    if entry_host != host_of(ctx.url) and entry_host not in ctx.git_hosts:
        raise MarketError("market.host_not_allowed")
    official = ctx.cls == "official"
    taken = official and entry["id"] in ctx.builtin and entry["id"] not in ctx.moved
    # the sandbox's test marketplace (dev) may carry the official ids to prove the real packages before the official
    # index is signed (G6, deviation from G3 C5); built-in ids stay refused, and a non-dev workspace never has the class
    ids = ctx.builtin if ctx.cls == "dev" else ctx.builtin | ctx.official_ids
    claimed = not official and entry["id"] in ids
    if taken or claimed:
        raise MarketError("market.reserved_id")
    prior = ctx.installed.get(entry["id"])
    if prior is not None and prior["origin"] != ctx.url:
        raise MarketError("market.origin_changed")
    if (
        prior is not None
        and prior["version"] == entry["version"]
        and prior["package_sha256"] != entry["package_sha256"]
    ):
        raise MarketError("market.version_reused")  # the same version number must be the same bytes
    floor = [v for v in (prior and prior["version"], ctx.seen.get(f"{ctx.url}|{entry['id']}")) if v]
    if any(semver(entry["version"]) < semver(v) for v in floor):
        raise MarketError("market.rollback")
    policy.evaluate("entry", ctx.hub, {"url": ctx.url, "id": entry["id"], "version": entry["version"]})  # block, allow
    pub = entry.get("publisher")
    if ctx.cls == "company":
        if ctx.hub is None:
            raise MarketError("market.not_allowed")  # a company class exists only under a hub policy
        policy.evaluate("publisher", ctx.hub, {"url": ctx.url, "publisher_key": None if pub is None else pub["key"]})
        if not index.verify_entry(entry, ctx.url):
            raise MarketError("market.bad_signature")
        tofu = "pinned"  # the hub's pin replaces TOFU for this URL: no TOFU read here, none written at the grant
    else:
        if pub is None and ctx.keys:
            raise MarketError("market.official_unsigned")
        if pub is not None and ctx.keys and pub["key"] not in ctx.keys:
            raise MarketError("market.official_unsigned")
        if pub is not None and not index.verify_entry(entry, ctx.url):
            raise MarketError("market.bad_signature")
        known = ctx.known_keys.get(f"{ctx.url}|{entry['id']}")
        if not key_change_ok(ctx.cls, ctx.keys, known, None if pub is None else pub["key"]):
            raise MarketError("market.publisher_key_changed")
        # a rotated official key counts as a first use of that key: shown as new, remembered when the grant is signed
        tofu = "unsigned" if pub is None else ("known" if known == pub["key"] else "new")
    policy.evaluate("manifest", ctx.hub, {"url": ctx.url, "capabilities": entry.get("capabilities", [])})  # step 8
    need, have = _core_key(entry["min_orch"]), _core_key(ctx.core_version)
    if need is None or have is None or have < need:
        raise MarketError("market.min_orch")
    return Facts(dict(entry), tofu, None if pub is None else pub["key"], prior)


def check_package(facts: Facts, pkg: Package, core_version: str) -> Manifest:
    """Steps 13 to 16 on the bytes read once. ``market.digest_mismatch``, ``market.manifest_mismatch`` (``runtime`` of
    the manifest and of the entry must be equal, both present or both absent), ``market.lock_missing`` and
    ``market.lock_invalid`` (step 15: the lockfile the MANIFEST names, nothing else is ever a lockfile), and the
    existing incompatibility (``market.min_orch`` is the entry's; a package that does not fit this core is
    ``market.manifest_mismatch`` here, never a silent install)."""
    entry = facts.entry
    if pkg.digest != entry["package_sha256"]:
        raise MarketError("market.digest_mismatch")
    try:
        manifest = load_manifest(pkg.files[MANIFEST_FILE])
    except (canon.JcsError, ValueError, ManifestError, RecursionError, UnicodeError):
        raise MarketError("market.manifest_mismatch") from None
    doc = manifest.doc
    if not (
        doc["schema"] == "orch.addon/3"
        and manifest.name == entry["id"]
        and manifest.version == entry["version"]
        and manifest.capabilities == entry["capabilities"]
        and doc.get("min_core") == entry["min_orch"]
        and doc.get("runtime") == entry.get("runtime")  # the manifest decides; the index copy must agree
    ):
        raise MarketError("market.manifest_mismatch")
    if manifest.runtime is not None:
        read_lock(manifest, pkg)  # step 15: MarketError lock_missing / lock_invalid
        if manifest.runtime["kind"] == "node-package-lock" and any(
            p.split("/")[0] == "node_modules" for p in pkg.files
        ):
            raise MarketError("market.package_invalid")  # bundled code is code the lock does not list
    if incompatibility(manifest, core_version) is not None:
        raise MarketError("market.manifest_mismatch")
    return manifest


# ------------------------------------------------------------------------------------------------ the update diff

_DOC_KEYS = ("entry", "cli", "skills", "agents_md")


def _set_by(f: Mapping[str, Any]) -> set[str]:
    return set(f.get("set_by", ()))


def diff(
    old: Manifest | None,
    new: Manifest,
    prev: Mapping[str, Any],
    facts: Facts,
    old_plan: Sequence[Planned] | None = None,
    new_plan: Sequence[Planned] | None = None,
) -> dict[str, Any]:
    """What the update changes against the add-on that is installed (``old``, read from its directory; ``None`` when it
    cannot be read: everything then counts as widened) and the install record ``prev``. ``changes`` are lines for a
    person; ``widened`` is True when the new package may do more than the old one: a capability, a field (or a wider
    ``set_by`` or gate change), a section or a wider ticket-type list, a needs rule, a CLI or agent hint, an artifact
    kind, a new entry command or skills, or a publisher key seen for the first time, or (V3 G4, C6) a dependency
    runtime that is added or changes kind, or any locked package that is added or re-versioned (removals do not widen).
    ``old_plan``/``new_plan`` are the planned rows of each side for this computer (``None``: no lock, or the old one is
    unreadable); a package whose install status or flags change counts as changed, not unchanged.
    The host labels it as index-derived where it comes from the index."""
    ev_new = declarations(new)
    added: dict[str, list[str]] = {k: [] for k in ("capabilities", "fields", "sections", "needs", "artifact_kinds")}
    removed: dict[str, list[str]] = {k: [] for k in added}
    changes: list[str] = []
    widened = old is None
    if old is not None:
        ev_old = declarations(old)
        if old.version != new.version:
            changes.append(f"version {old.version} -> {new.version}")
        oc, nc = set(old.capabilities), set(new.capabilities)
        added["capabilities"], removed["capabilities"] = sorted(nc - oc), sorted(oc - nc)
        for f in sorted(set(ev_old["fields"]) | set(ev_new["fields"])):
            a, b = ev_old["fields"].get(f), ev_new["fields"].get(f)
            if a is None:
                added["fields"].append(f)
            elif b is None:
                removed["fields"].append(f)
            elif a != b:
                changes.append(f"field {f} changed")
                if not _set_by(b) <= _set_by(a) or set(b.get("gate", ())) != set(a.get("gate", ())):
                    widened = True
        so, sn = {s["id"]: s for s in ev_old["sections"]}, {s["id"]: s for s in ev_new["sections"]}
        added["sections"], removed["sections"] = sorted(set(sn) - set(so)), sorted(set(so) - set(sn))
        for sid in sorted(set(so) & set(sn)):
            if so[sid] != sn[sid]:
                changes.append(f"section {sid} changed")
                widened = widened or not set(sn[sid]["types"]) <= set(so[sid]["types"])
        ro, rn = {r["id"] for r in old.needs}, {r["id"] for r in new.needs}
        added["needs"], removed["needs"] = sorted(rn - ro), sorted(ro - rn)
        if any(r not in old.needs for r in new.needs):
            widened = True  # a new or changed needs rule
        ko, kn = set(ev_old["artifact_kinds"]), set(ev_new["artifact_kinds"])
        added["artifact_kinds"], removed["artifact_kinds"] = sorted(kn - ko), sorted(ko - kn)
        if ev_old.get("events") != ev_new.get("events"):
            changes.append("events changed")
            widened = True  # what the add-on may wake a follow-up with: the person reads it again
        for k in _DOC_KEYS:
            if old.doc.get(k) != new.doc.get(k):
                changes.append(f"{k.replace('_', ' ')} changed")
                widened = True  # a new command, hint, CLI or skills: what the add-on tells agents or runs
        widened = widened or any(added[k] for k in ("capabilities", "fields", "sections", "needs", "artifact_kinds"))
    dep = deps.diff(
        None if old is None or old.runtime is None else old.runtime["kind"],
        old_plan,
        None if new.runtime is None else new.runtime["kind"],
        new_plan,
    )
    changes += dep["lines"]
    widened = widened or dep["widened"]
    old_key = prev.get("publisher_key")
    if facts.tofu == "unsigned":
        publisher = "unsigned"
    elif facts.tofu == "pinned":
        publisher = "pinned"  # the hub pins the key: a change of key is the hub's, and shows as a refusal, not a diff
    elif old_key is None or old_key != facts.publisher_key:
        publisher = "first_use"  # a key seen for the first time, also after an official key rotation (#1191)
        widened = True
        if old_key is not None:
            changes.append(ROTATED)
    else:
        publisher = "same"
    for name, lst in added.items():
        changes += [f"+ {x} ({name.replace('_', ' ')}, new)" for x in lst]
    for name, lst in removed.items():
        changes += [f"- {x} ({name.replace('_', ' ')}, removed)" for x in lst]
    return {
        "from": {k: prev.get(k) for k in ("version", "package_sha256", "commit")},
        "changes": changes,
        "added": added,
        "removed": removed,
        "widened": widened,
        "publisher": publisher,
        "deps": {k: dep[k] for k in ("runtime", "added", "removed", "changed", "lines")},
    }
