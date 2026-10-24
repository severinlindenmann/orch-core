"""Installing, updating and removing an add-on from a marketplace, host side (V3 G3, #921; spec sections 5 to 7;
design ``state/design-g3.md`` with conditions C1 to C6).

Ops (all human-only except ``market.installed``; the dispatch column plus :func:`orch.host.marketops._check` refuse an
agent or a mandate before anything is read or fetched):

* ``market.stage {id, url?, update?}``: steps 0 to 16 of spec 5 for one entry. Steps 0 to 10 and the class gate run on
  the workspace worker without network; the fetch and the checks on the bytes (11 to 16) run off the worker with no lock
  held. Nothing is written into ``addons/`` and no record changes: the result is ONE stage file
  (``<state>/hosts/<ws>/market/staged/<stage>.json``) holding the package that was read once, its digest and the
  facts of the review. A reviewed stage is signed through the existing ``addon.grant {name, staged}``.
* ``market.unstage {stage}``, ``market.installed {}``, ``market.uninstall {name}``.
* ``market.dev_stage {path, rev?, subdir?}`` (V3 G5, ``orch addon dev``): the same stage for a commit of a LOCAL
  repository, only in a dev workspace on a dev host (:func:`orch.host.devmode.dev_gate`, asked on the worker and again
  at submit). It is not a marketplace source: no index, no class, no origin, no publisher key; the signed grant is the
  same ``addon.grant {staged}`` and installs the same bytes the same way, but :func:`_converge` writes NO install
  record, rollback floor or key, so nothing is ever "installed from" the folder and a plain re-grant of it is refused.

The signed grant is the only thing that installs: :func:`recheck` runs under the store lock before the journal write
(C2), :func:`after_grant` runs from ``human.finish`` after the append (so start-up roll-forward runs it too, idempotent,
C3) and swaps the directory by digest, then writes the install record, the rollback floor and the publisher key. The
install record and the TOFU key change only there."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from orch import __version__ as CORE_VERSION
from orch.addons import builtin, credential, install, lockfile
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError, from_files, read_package
from orch.market import ADDONS, MarketError, constants, deps, fetch, index, policy
from orch.market import install as mi
from orch.market.url import canonical_market_url
from orch.ops.errors import ERRORS

from . import addonenv, hubpolicy
from .errors import HostError
from .marketstate import MarketFiles

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = [
    "HUMAN_ONLY",
    "MARKET_OPS",
    "NETWORK_OPS",
    "OP_CODES",
    "READS",
    "SCHEMAS",
    "DEV_NOTE",
    "SOURCE_NOTE",
    "after_grant",
    "files_of",
    "gate_plain",
    "market_class",
    "recheck",
    "recover",
    "run",
    "serve",
    "staged_package",
    "state_of",
    "verdict",
]

LOG = logging.getLogger("orch.host.marketinstall")
MARKET_OPS: dict[str, int] = {
    "market.stage": 1, "market.unstage": 1, "market.installed": 1, "market.uninstall": 1, "market.env_build": 1,
    "market.dev_stage": 1,
}  # fmt: skip
READS = frozenset({"market.installed"})
# the fetch / the git read / the dependency downloads: off the worker, no lock held
NETWORK_OPS = frozenset({"market.stage", "market.dev_stage", "market.env_build"})
HUMAN_ONLY = frozenset({"market.stage", "market.unstage", "market.uninstall", "market.dev_stage", "market.env_build"})
SOURCE_NOTE = "source as claimed by the marketplace"
DEV_NOTE = "a local folder at this commit (orch addon dev), not a marketplace"
_STAGE_CODES = (
    "market.actor_refused", "market.state_invalid", "market.malformed", "market.community_unisolated",
    "market.not_allowed", "market.unknown_addon", "market.ambiguous_addon", "market.not_installed", "market.revoked",
    "market.host_not_allowed", "market.reserved_id", "market.origin_changed", "market.version_reused",
    "market.rollback", "market.official_unsigned", "market.bad_signature", "market.publisher_key_changed",
    "market.min_orch", "market.address_refused", "market.fetch_failed", "market.commit_mismatch",
    "market.package_invalid", "market.digest_mismatch", "market.manifest_mismatch", "market.index_unsigned",
    "market.source_mismatch", "market.unpinned", "market.bad_digest_format", "market.duplicate_id",
    "market.refresh_running", "market.capability_ceiling", "market.lock_missing", "market.lock_invalid",
    "market.env_failed", "market.blocked", "market.addon_not_allowed", "market.publisher_unpinned",
)  # fmt: skip
#: the codes each op can raise (the per-op declaration; a test checks every raise site against it)
OP_CODES: dict[str, tuple[str, ...]] = {
    "market.stage": _STAGE_CODES,
    "market.unstage": ("market.actor_refused", "market.state_invalid", "market.malformed"),
    "market.installed": ("market.state_invalid", "market.malformed"),
    "market.env_build": (
        "market.actor_refused",
        "market.state_invalid",
        "market.not_installed",
        "market.env_failed",
        "market.lock_missing",
        "market.lock_invalid",
        "market.address_refused",
        "market.fetch_failed",
    ),
    "market.dev_stage": (
        "market.actor_refused",
        "market.state_invalid",
        "market.reserved_id",
        "market.origin_changed",
        "market.fetch_failed",
        "market.package_invalid",
        "market.manifest_mismatch",
        "host.dev_refused",
        "market.lock_missing",
        "market.lock_invalid",
        "market.env_failed",
        "market.address_refused",
    ),
    "market.uninstall": (
        "market.actor_refused",
        "market.state_invalid",
        "market.not_installed",
        "market.still_enabled",
    ),
}
_NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}
SCHEMAS: dict[str, dict[str, Any]] = {
    "market.stage": {
        "type": "object",
        "properties": {
            "id": _NAME,
            "url": {"type": "string", "minLength": 1, "maxLength": 200},
            "update": {"type": "boolean"},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    "market.unstage": {
        "type": "object",
        "properties": {"stage": {"type": "string", "pattern": "^st_[0-9a-f]{32}$"}},
        "required": ["stage"],
        "additionalProperties": False,
    },
    "market.installed": {"type": "object", "additionalProperties": False},
    "market.env_build": {
        "type": "object", "properties": {"name": _NAME}, "required": ["name"], "additionalProperties": False
    },
    "market.dev_stage": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024, "pattern": "^/"},
            "rev": {"type": "string", "minLength": 1, "maxLength": 100},
            "subdir": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "market.uninstall": {
        "type": "object", "properties": {"name": _NAME}, "required": ["name"], "additionalProperties": False
    },
}  # fmt: skip


def _files(service: WorkspaceService) -> MarketFiles:
    return MarketFiles(Path(service.state_dir), service.workspace_id, service.host_uid)


def files_of(service: WorkspaceService) -> MarketFiles:
    """The marketplace records of this workspace (what a prepared ``addon.grant`` reads through its capture)."""
    return _files(service)


def _refuse(op: str, code: str, reason: str | None = None) -> HostError:
    from .marketops import _refuse as refuse

    return refuse(op, code, reason)


def _store_root(service: WorkspaceService) -> Path:
    return Path(service.store.layout.store)


def state_of(service: WorkspaceService, name: str) -> str:
    model = _model_addon(service, name)
    return "unknown" if model is None else "purged" if model.purged else "enabled" if model.enabled else "disabled"


def _model_addon(service: WorkspaceService, name: str) -> Any:
    return install.as_addons(service.store.state.workspace.addons).get(name)


# ------------------------------------------------------------------------------------------------ the class
def market_class(service: WorkspaceService, url: str, pol: Any) -> tuple[str, tuple[str, ...]]:
    """``(class, publisher/index keys)`` of the canonical ``url`` for this workspace under the hub policy ``pol``
    (``None`` standalone; :data:`orch.host.hubpolicy.UNREADABLE` refuses): the official marketplace, a ``company``
    marketplace the hub lists with its pinned keys (V3-D32), or the sandbox's test marketplace (C5: only a dev host's
    ``dev.json`` entry, only for a workspace that passes S1 to S6 now), else ``market.community_unisolated``. The one
    place the class is decided; runs on the worker thread."""
    from . import devmode

    if isinstance(pol, hubpolicy._Unreadable):  # noqa: SLF001
        raise MarketError("market.not_allowed")
    dev = devmode.test_market(service)
    cls = policy.gate(url, policy=pol, dev_url=None if dev is None else dev[0])
    if cls == "company":
        return cls, tuple(pol.market(url).keys)
    return cls, (dev[1] if cls == "dev" and dev is not None else constants.OFFICIAL_KEYS if cls == "official" else ())


def verdict(raw: bytes, url: str, cls: str, keys: tuple[str, ...], now: float) -> index.Verdict:
    """Steps 1 and 2 on the exact bytes, with the keys of the class (``official``, ``dev`` and the hub's ``company``
    need a signature; the last two by the keys they were given)."""
    return index.evaluate(
        raw, url, official=cls in ("official", "dev", "company"), now=now,
        keys=keys if cls in ("dev", "company") else None,
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------ serve (worker)
def serve(call: Call, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """The worker half. ``market.stage`` gates (policy, class, which marketplace) and returns the plan for
    :func:`run`; the others are answered here."""
    from .workspace import ok_result

    service = call.service
    try:
        if op == "market.installed":
            return ok_result(_installed(service))
        if op == "market.unstage":
            return ok_result({"removed": _files(service).drop_stage(args["stage"])})
        if op == "market.uninstall":
            return ok_result(_uninstall(service, args["name"]))
        if op == "market.env_build":
            return {"plan": _plan_build(service, args["name"])}
        if op == "market.dev_stage":
            return {"plan": _plan_dev(call, args)}
        return {"plan": _plan_stage(call, args)}
    except MarketError as exc:
        raise _refuse(op, exc.code, exc.reason) from None


def _plan_build(service: WorkspaceService, name: str) -> dict[str, Any]:
    """``market.env_build``, worker half: the latest grant of ``name`` (on, with ``runtime``) under the store lock."""
    from . import addonenv

    return {
        "op": "market.env_build",
        "id": name,
        "grant": addonenv.snapshot(service, name),
        "root": str(_store_root(service)),
    }


def _plan_dev(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """``market.dev_stage`` on the worker: the dev gate first (a dev host AND a workspace that passes S1 to S6 now),
    before any path is looked at. Everything else happens in :func:`run`, off the worker."""
    from . import devmode

    if devmode.dev_gate(call.service) is None:
        raise devmode.refused(
            "not_sandbox",
            "orch addon dev works only in a dev sandbox workspace on a dev host",
            hint="a real workspace installs add-ons from a marketplace: orch addon install",
        )
    service = call.service
    return {
        "op": "market.dev_stage", "id": None, "path": args["path"], "rev": args.get("rev"),
        "subdir": args.get("subdir"), "root": str(_store_root(service)), "now": service.clock(),
        "granted": dict(install.as_addons(service.store.state.workspace.addons)), "person": call.viewer,
    }  # fmt: skip


def _plan_stage(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    pol = hubpolicy.stored(service)  # P0: after the fresh read of execute(); a joined host that cannot verify refuses
    files = _files(service)
    records = files.load()
    installs = files.installs()
    name = args["id"]
    prior = installs[ADDONS].get(name)
    update = bool(args.get("update"))
    if update and prior is None:
        raise MarketError("market.not_installed")
    url = args.get("url")
    if update:
        if url is not None and url != prior["origin"]:
            raise MarketError("market.origin_changed")
        url = prior["origin"]
    if url is not None:
        canonical_market_url(url)
        if url not in records:
            raise HostError("not_found", "that marketplace was not added: orch market list")
    else:
        url = _find_market(files, records, service, name, prior, pol)
    cls, keys = market_class(service, url, pol)  # step 5 (listed) and step 4 (class), before any read of the market
    return {
        "op": "market.stage", "url": url, "id": name, "update": update or prior is not None, "cls": cls, "keys": keys,
        "root": str(_store_root(service)), "now": service.clock(), "previous": _model_addon(service, name),
        "person": call.viewer,
        "policy": pol,
    }  # fmt: skip


def _find_market(files: MarketFiles, records: dict, service: WorkspaceService, name: str, prior: Any, pol: Any) -> str:
    """The one added marketplace that lists ``name`` (from the cached, re-verified index): none is
    ``market.unknown_addon``, more than one ``market.ambiguous_addon``. An installed add-on is its origin's."""
    if prior is not None:
        return prior["origin"]
    found, denied = [], False
    for url in sorted(records):
        try:
            cls, keys = market_class(service, url, pol)
            raw = files.read_cache(url)
            if raw is None:
                continue
            v = verdict(raw, url, cls, keys, service.clock())
        except MarketError as exc:
            denied = denied or exc.code == "market.not_allowed"
            continue
        if any(e["id"] == name for e in v.doc[ADDONS]):
            found.append(url)
    if not found:
        raise MarketError("market.not_allowed" if denied else "market.unknown_addon")  # the hub's word, not "unknown"
    if len(found) > 1:
        raise MarketError("market.ambiguous_addon")
    return found[0]


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(run, call, planned["plan"])


def run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    service = call.service
    op = plan["op"]
    if op == "market.env_build":
        return _env_build(call, plan)
    try:
        out = _dev_stage(service, plan) if op == "market.dev_stage" else _stage(service, plan, call)
    except MarketError as exc:
        _audit(call, f"{op}.refused", id=plan["id"], code=exc.code)
        raise _refuse(op, exc.code, exc.reason) from None
    if op == "market.dev_stage":
        _audit(call, op, id=out["name"], stage=out["stage"])
    else:
        _audit(call, op, id=plan["id"], stage=out.get("stage"), up_to_date=out.get("up_to_date", False))
    return ok_result(out)


def _env_build(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from . import addonenv
    from .workspace import ok_result

    try:
        out = addonenv.build(call.service, plan["id"], plan["grant"], plan["root"])
    except MarketError as exc:
        _audit(call, "market.env_build.refused", id=plan["id"], code=exc.code, reason=exc.reason or "")
        raise _refuse("market.env_build", exc.code, exc.reason) from None
    _audit(call, "market.env_build", id=plan["id"], count=out["count"])
    return ok_result(out)


def _audit(call: Call, event: str, **more: Any) -> None:
    from . import hubops

    hubops._audit(call, event, **more)  # noqa: SLF001


def _stage(service: WorkspaceService, plan: dict[str, Any], call: Call | None = None) -> dict[str, Any]:
    """Steps 1 to 16, then the stage file. Raises :class:`MarketError`; nothing but the stage file is ever written."""
    from . import marketops

    files = _files(service)
    url, name, cls, keys, now = plan["url"], plan["id"], plan["cls"], tuple(plan["keys"]), plan["now"]
    installs = files.installs()
    prior_installed = installs[ADDONS].get(name)
    if plan["update"] and prior_installed is not None:
        rec = files.load().get(url)
        if rec is None:
            raise MarketError("market.state_invalid")
        if not rec["pinned"]:  # an update refreshes the index of its origin (a person's, so no agent interval)
            marketops._refresh(files, url, None, False, service.clock, cls, keys)  # noqa: SLF001
    rec = files.load().get(url)
    if rec is None:
        raise MarketError("market.state_invalid")
    raw = files.read_cache(url)
    if raw is None or marketops._sha(raw) != rec["index_sha256"]:  # noqa: SLF001
        raise MarketError("market.malformed")
    v = verdict(raw, url, cls, keys, now)
    pol = plan.get("policy")
    policy.evaluate("index", pol, {"url": url, "index_key": (v.doc.get("signature") or {}).get("key")})  # step 2
    index.check_floor(rec["last_version"], rec["payload_sha256"], v)
    ctx = mi.Ctx(
        url=url, cls=cls, keys=keys, core_version=CORE_VERSION, git_hosts=frozenset(constants.DEFAULT_GIT_HOSTS),
        installed=installs[ADDONS], known_keys=installs["keys"], seen=installs["seen"],
        official_ids=frozenset(constants.OFFICIAL_IDS), moved=frozenset(constants.MOVED_IDS), hub=pol,
    )  # fmt: skip
    facts = mi.check_entry(ctx, v.doc, name)  # steps 6 to 9, the hub's block, allow, pin and ceiling in their places
    prior = facts.prior
    entry = facts.entry
    if (
        prior is not None
        and prior["version"] == entry["version"]
        and prior["package_sha256"] == entry["package_sha256"]
    ):
        return {"up_to_date": True, "name": name, "version": entry["version"]}
    LOG.warning("market.stage fetch %s@%s from %s", name, entry["version"], entry["source"]["git"])  # S13 greps this
    pkg = fetch.fetch_package(entry)
    manifest = mi.check_package(facts, pkg, CORE_VERSION)  # steps 13 to 16, the lockfile (15) included
    policy.evaluate(  # step 14: the policy again, on the manifest of the bytes that were read (the index is a claim)
        "grant", pol,
        {"url": url, "id": name, "version": manifest.version, "capabilities": manifest.capabilities,
         "publisher_key": facts.publisher_key},
    )  # fmt: skip
    event = install.grant_event(manifest, pkg)
    planned, dep_view = _dependencies(service, manifest, pkg)
    review = install.review_lines(manifest, pkg, event, plan["previous"], dep_view)
    change = None
    if prior is not None:
        old_plan = None
        try:
            opkg, old = install.read_installed(plan["root"], name)
            if plan["previous"] is None or opkg.digest != plan["previous"].package_sha256:
                old = None  # not the bytes of the signed grant: everything counts as widened (review F1)
            elif old.runtime is not None:
                with contextlib.suppress(MarketError):  # unreadable old lock: every row of the new one counts as new
                    old_plan = lockfile.plan(install.read_lock(old, opkg), lockfile.current_host())
        except (PackageError, ManifestError):
            old = None
        change = mi.diff(old, manifest, prior, facts, old_plan, planned)
        if dep_view is not None or change["deps"]["runtime"] is not None:
            diff = {"lines": list(change["deps"]["lines"]), "widened": bool(change["widened"])}
            if dep_view is not None:
                dep_view["diff"] = diff  # carried to the signer: host-derived, labelled, not signed
        review += [f"update: {x}" for x in change["changes"]]
        review.append("widened: yes, read all of it" if change["widened"] else "widened: no")
    stage = "st_" + secrets.token_hex(16)
    expires = now + constants.STAGE_TTL
    market = {
        "url": url, "class": cls, "commit": entry["commit"], "source_note": SOURCE_NOTE,
        "publisher": {
            "fingerprint": None if facts.publisher_key is None else marketops.fingerprint(facts.publisher_key),
            "tofu": facts.tofu,
        },
        "index_version": v.doc["version"], "stale": v.stale, "summary": entry.get("summary"),
    }  # fmt: skip
    files.write_stage(
        {
            "v": 1, "stage": stage, "name": name, "version": entry["version"], "digest": pkg.digest,
            "origin": url, "commit": entry["commit"], "publisher_key": facts.publisher_key, "tofu": facts.tofu,
            "index_version": v.doc["version"], "prev_digest": None if prior is None else prior["package_sha256"],
            "created_at": now, "expires_at": expires, "class": cls, "summary": entry.get("summary"),
            "stale": v.stale,
            "entry": {k: entry[k] for k in ("id", "version", "capabilities", "min_orch", "runtime") if k in entry},
            "deps": dep_view,
            "files": {p: base64.b64encode(b).decode("ascii") for p, b in pkg.files.items()},
        }
    )  # fmt: skip
    body = {k: val for k, val in event.items() if k != "type"}
    return {
        "stage": stage, "expires_at": int(expires), "name": name, "version": entry["version"],
        "package_sha256": pkg.digest, "grant": body, "review": review, "market": market, "update": change,
        "deps": dep_view,
    }  # fmt: skip


def _dependencies(service: WorkspaceService, manifest: Any, pkg: Any) -> tuple[Any, dict[str, Any] | None]:
    """``(planned rows, deps view)`` of a package that declares ``runtime`` (the lock was checked at step 15), else
    ``(None, None)``. The plan decides what this computer installs: a lock that cannot be installed here is refused
    now (``market.env_failed``), before the person signs."""
    if manifest.runtime is None:
        return None, None
    lock = install.read_lock(manifest, pkg)
    planned = lockfile.plan(lock, lockfile.current_host())
    found = addonenv.stage_fetch(service, lock, planned)  # D3: download and verify now, before the signature
    return planned, deps.view(manifest.runtime["lock"], lock, planned, found)


def _dev_stage(service: WorkspaceService, plan: dict[str, Any]) -> dict[str, Any]:
    """``orch addon dev``: a commit of a local repository, read with the closed local export (the same bytes
    ``orch addon pack`` hashes), judged by the checks an install runs on the package, then ONE stage file marked
    ``dev``. No index, no marketplace, no key: the stage names no origin and the grant records no install."""
    from orch import gitsafe
    from orch.market import author

    try:
        commit, files = gitsafe.market_local_export(plan["path"], plan["rev"], plan["subdir"])
        pkg, manifest = author.check_files(files)
    except gitsafe.MarketGitError as exc:
        raise MarketError("market.package_invalid" if exc.kind == "package" else "market.fetch_failed") from None
    except gitsafe.Unsafe as exc:
        raise HostError("invalid.input", f"cannot read that repository: {str(exc)[:200]}") from None
    except author.AuthorError as exc:
        raise MarketError(exc.code if exc.code.startswith("market.") else "market.package_invalid") from None
    name = manifest.name
    if name in builtin.NAMES or name in constants.OFFICIAL_IDS or name in constants.MOVED_IDS:
        raise MarketError("market.reserved_id")  # a dev folder never takes the name of a built-in or official add-on
    files_ = _files(service)
    if name in files_.installs()[ADDONS]:
        raise MarketError("market.origin_changed")  # installed from a marketplace: dev never replaces it
    event = install.grant_event(manifest, pkg)
    _, dep_view = _dependencies(service, manifest, pkg)  # a dev folder with dependencies is judged and fetched alike
    review = install.review_lines(manifest, pkg, event, plan["granted"].get(name), dep_view)
    now = plan["now"]
    stage = "st_" + secrets.token_hex(16)
    expires = now + constants.STAGE_TTL
    files_.write_stage(
        {
            "v": 1, "stage": stage, "dev": True, "name": name, "version": manifest.version, "digest": pkg.digest,
            "origin": None, "commit": commit, "publisher_key": None, "tofu": "unsigned", "index_version": 0,
            "prev_digest": None, "created_at": now, "expires_at": expires, "class": "dev-folder", "summary": None,
            "stale": False,
            "entry": {
                "id": name, "version": manifest.version, "capabilities": manifest.capabilities,
                "min_orch": manifest.min_core, **({"runtime": manifest.runtime} if manifest.runtime else {}),
            },
            "deps": dep_view,
            "files": {p: base64.b64encode(b).decode("ascii") for p, b in pkg.files.items()},
        }
    )  # fmt: skip
    body = {k: val for k, val in event.items() if k != "type"}
    return {
        "stage": stage, "expires_at": int(expires), "name": name, "version": manifest.version, "commit": commit,
        "package_sha256": pkg.digest, "grant": body, "review": review, "dev": True,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ read the stage
def staged_package(files: MarketFiles, name: str, stage: str, now: float) -> Any:
    """For ``addon.grant {staged}``: ``(stage doc, Package, Manifest)`` re-read from the stage file once, or
    ``market.staged_changed``. The digest must equal the stored one, the manifest must agree with the stored entry
    facts (steps 14 and 16 again on these bytes), the stage must be the add-on's and not expired."""
    doc = files.read_stage(stage)
    if doc is None or doc.get("name") != name or not isinstance(doc.get("expires_at"), (int, float)):
        raise MarketError("market.staged_changed")
    if now > doc["expires_at"]:
        raise MarketError("market.staged_changed")
    try:
        pkg = from_files({p: base64.b64decode(b, validate=True) for p, b in doc["files"].items()})
        if pkg.digest != doc["digest"]:
            raise MarketError("market.staged_changed")
        facts = mi.Facts(
            {**doc["entry"], "package_sha256": doc["digest"]}, doc["tofu"], doc["publisher_key"], None
        )  # the entry as it was when it passed steps 1 to 10
        manifest = mi.check_package(facts, pkg, CORE_VERSION)
    except (PackageError, ValueError, KeyError, TypeError):
        raise MarketError("market.staged_changed") from None
    return doc, pkg, manifest


# ------------------------------------------------------------------------------------------------ C2: recheck
def recheck(service: WorkspaceService, record: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Under ``store.locked()`` in ``human.submit``, on the re-read state, BEFORE the journal write (C2): an
    ``addon.grant`` is judged again against the install record as it is now. Raises :class:`HostError`."""
    if record["op"] != "addon.grant":
        return
    ev = events[0]
    try:
        _recheck(service, record, ev)
    except MarketError as exc:
        raise HostError(exc.code, ERRORS[exc.code].message if exc.code in ERRORS else exc.code) from None


def _recheck(service: WorkspaceService, record: dict[str, Any], ev: dict[str, Any]) -> None:
    files = _files(service)
    staged = record.get("staged")
    if staged is None:  # C1 again at submit: built-in bytes or the bytes of the install record
        gate_plain(files, ev)
        pol = hubpolicy.stored(
            service
        )  # the same grant-stage evaluation as a staged grant, on the then-pinned snapshot
        if pol is not None:  # no record is a real built-in only (hubpolicy.check): a deleted record is not a built-in
            grant = SimpleNamespace(
                name=ev.get("name"), version=ev.get("version"), package_sha256=ev.get("package_sha256"),
                capabilities=ev.get("capabilities") or [],
            )  # fmt: skip
            hubpolicy.check(service, pol, grant, files.installs()[ADDONS].get(ev.get("name")))
        return
    doc, pkg, _ = staged_package(files, ev["name"], staged["stage"], service.clock())
    if pkg.digest != ev["package_sha256"] or doc["version"] != ev["version"]:
        raise MarketError("market.staged_changed")
    if doc.get("dev") is True:  # a dev folder: no index to re-read; the dev gate and the name, as they are now
        _recheck_dev(service, files, ev)
        return
    url, version = doc["origin"], ev["version"]
    from . import marketops  # the index and the class as they are now (review F2)

    rec, raw = files.load().get(url), files.read_cache(url)
    if rec is None or raw is None or marketops._sha(raw) != rec["index_sha256"]:  # noqa: SLF001
        raise MarketError("market.staged_changed")
    pol = hubpolicy.stored(service)  # the then-pinned snapshot, no network (step 18, under the store lock)
    cls, keys = market_class(service, url, pol)
    if cls != doc.get("class"):
        raise MarketError("market.staged_changed")
    now_doc = verdict(raw, url, cls, keys, service.clock()).doc
    if f"{ev['name']}@{version}" in now_doc.get("revoked", []):
        raise MarketError("market.revoked")
    listed = next((e for e in now_doc[ADDONS] if e["id"] == ev["name"]), None)
    if listed is None or listed["package_sha256"] != pkg.digest or now_doc["version"] < doc["index_version"]:
        raise MarketError("market.staged_changed")  # D1: the index still lists these bytes, and is not older
    installs = files.installs()
    prior = installs[ADDONS].get(ev["name"])
    if prior is not None:
        if prior["origin"] != url:
            raise MarketError("market.origin_changed")
        if prior["version"] == version and prior["package_sha256"] != pkg.digest:
            raise MarketError("market.version_reused")
    floor = [x for x in (prior and prior["version"], installs["seen"].get(f"{url}|{ev['name']}")) if x]
    if any(mi.semver(version) < mi.semver(x) for x in floor):
        raise MarketError("market.rollback")
    known = installs["keys"].get(f"{url}|{ev['name']}")
    if cls != "company" and not mi.key_change_ok(cls, keys, known, doc["publisher_key"]):  # hub pin replaces TOFU
        raise MarketError("market.publisher_key_changed")
    policy.evaluate(
        "grant", pol,
        {"url": url, "id": ev["name"], "version": version, "capabilities": ev["capabilities"],
         "publisher_key": doc["publisher_key"]},
    )  # fmt: skip


def _recheck_dev(service: WorkspaceService, files: MarketFiles, ev: dict[str, Any]) -> None:
    """Step 18 for a dev stage (inside ``store.locked()``): the workspace must still pass the dev gate and the
    name must still be free of a marketplace install and of a reserved name."""
    from . import devmode

    if devmode.dev_gate(service) is None:
        raise devmode.refused("not_sandbox", "orch addon dev works only in a dev sandbox workspace on a dev host")
    name = ev["name"]
    if name in builtin.NAMES or name in constants.OFFICIAL_IDS or name in constants.MOVED_IDS:
        raise MarketError("market.reserved_id")
    if name in files.installs()[ADDONS]:
        raise MarketError("market.origin_changed")
    pol = hubpolicy.stored(service)  # a dev folder has no origin: block and ceiling still apply under a hub policy
    if pol is not None:
        policy.check_run(
            pol, name=name, version=ev.get("version"), capabilities=ev.get("capabilities") or [], origin=None, key=None
        )


def gate_plain(files: MarketFiles, ev: dict[str, Any]) -> None:
    """C1: an ``addon.grant`` without ``staged`` signs only the bytes of a built-in add-on or the bytes the install
    record of that name holds (a re-enable after a disable). Anything else, such as a folder copied into ``addons/``, is
    ``market.not_installed``."""
    if builtin.is_builtin_package(ev, ev.get("name")):
        return
    rec = files.installs()[ADDONS].get(ev.get("name"))
    if rec is None or rec["package_sha256"] != ev.get("package_sha256") or rec["version"] != ev.get("version"):
        raise MarketError("market.not_installed")


# ------------------------------------------------------------------------------------------------ C3: after the append
def after_grant(service: WorkspaceService, record: dict[str, Any], signed: list[dict[str, Any]]) -> None:
    """From ``human.finish``, after the signed ``addon.granted`` is in the log (also when start-up rolls a submission
    forward): put the granted bytes in place and write the records. Idempotent: it decides by digests, never by names,
    and a second run finds nothing to do. Failures are logged, never raised: the registry already fails closed on a
    directory whose digest is not the grant's, and :func:`recover` finishes it at the next start."""
    sweep_credentials(service)  # V3 G6 C4: a purge or a grant that dropped the capability deletes the key
    staged = record.get("staged")
    if record.get("op") != "addon.grant" or staged is None or not signed:
        return
    try:
        _converge(service, staged["stage"], signed[0])
    except Exception:  # noqa: BLE001 - fail closed: the digest check of the registry holds the add-on back
        LOG.exception("market install: finishing the swap of %s failed", staged.get("stage"))


def sweep_credentials(service: WorkspaceService) -> None:
    """Delete the stored key (:mod:`orch.addons.credential`) of every add-on that is not granted, is purged, or whose
    ACTIVE grant no longer holds ``credential`` (V3 G6 C4). Idempotent; failures are logged, never raised: a key that
    stays is only read while the grant qualifies (C2)."""
    try:
        keep = [
            n
            for n, g in install.as_addons(service.store.state.workspace.addons).items()
            if not g.purged and "credential" in g.capabilities
        ]
        credential.sweep(service.state_dir, service.workspace_id, keep)
    except Exception:  # noqa: BLE001
        LOG.exception("add-on credential sweep failed")


def _converge(service: WorkspaceService, stage: str, ev: dict[str, Any]) -> None:
    files = _files(service)
    name, digest = ev["name"], ev["package_sha256"]
    root = _store_root(service)
    addons = root / install.ADDONS_DIR
    doc = files.read_stage(stage)
    pkg = None
    if doc is not None:
        with contextlib.suppress(PackageError, ValueError):
            pkg = from_files({p: base64.b64decode(b) for p, b in doc["files"].items()})
        if pkg is not None and pkg.digest != digest:
            pkg = None
    with _addons_dir(addons) as d:
        _swap(d, name, digest, pkg)
        if _digest_of(d, name) != digest:
            LOG.error("market install: %s is not in place after the swap", name)
            return
        _sweep(d, name)
    if doc is None or pkg is None:
        return
    if (
        doc.get("dev") is True
    ):  # never recorded as an install: no origin, floor or key (the dev stage is the only trace)
        files.drop_stage(stage)
        return
    with files.commit_installs() as write:
        cur = files.installs()
        cur[ADDONS][name] = {
            "origin": doc["origin"], "version": doc["version"], "package_sha256": digest, "commit": doc["commit"],
            "installed_at": int(doc["created_at"]), "publisher_key": doc["publisher_key"],
        }  # fmt: skip
        key = f"{doc['origin']}|{name}"
        old = cur["seen"].get(key)
        if old is None or mi.semver(doc["version"]) > mi.semver(old):
            cur["seen"][key] = doc["version"]
        pk = doc["publisher_key"]
        if pk is not None and doc.get("class") == "official" and pk in index.official_keys():
            cur["keys"][key] = pk  # an official key rotation moves the record (#1191); only to a key this release pins
        elif pk is not None and doc.get("tofu") != "pinned":  # the hub's pin replaces TOFU: none stored
            cur["keys"].setdefault(key, pk)
        write(cur)
    files.drop_stage(stage)


class _Dir:
    """``<store>/addons`` opened without following a link: every operation below goes through this descriptor, so a
    directory swapped for a link while we work is never followed. ``path`` is used only to reserve fresh names."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path, self.fd = path, fd


@contextlib.contextmanager
def _addons_dir(addons: Path):
    addons.mkdir(mode=0o755, exist_ok=True)
    fd = os.open(addons, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yield _Dir(addons, fd)
    finally:
        os.close(fd)


def _digest_of(d: _Dir, name: str) -> str | None:
    try:
        return read_package(name, dir_fd=d.fd).digest
    except (PackageError, OSError):
        return None


def _exists(d: _Dir, name: str) -> bool:
    try:
        os.stat(name, dir_fd=d.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _siblings(d: _Dir, name: str, kind: str) -> list[str]:
    prefix = f".{name}.{kind}-"
    return sorted(n for n in os.listdir(d.fd) if n.startswith(prefix))


def _swap(d: _Dir, name: str, digest: str, pkg: Any) -> None:
    """Make ``<name>`` hold the package with ``digest``: it already does; or a finished ``.<name>.new-*`` does (a crash
    between the renames); or the bytes are materialized into a fresh ``.<name>.new-*`` first. The old directory is moved
    aside (to a name reserved with mkdtemp) and removed after. Same directory throughout: renames only."""
    if _digest_of(d, name) == digest:
        return
    new = next((n for n in _siblings(d, name, "new") if _digest_of(d, n) == digest), None)
    if new is None:
        if pkg is None:
            return  # nothing to put in place: the registry keeps failing closed
        new = _materialize(d, name, pkg)
    if _exists(d, name):
        old = os.path.basename(tempfile.mkdtemp(prefix=f".{name}.old-", dir=d.path))
        os.rename(name, old, src_dir_fd=d.fd, dst_dir_fd=d.fd)  # onto the empty reserved directory
    os.rename(new, name, src_dir_fd=d.fd, dst_dir_fd=d.fd)


def _materialize(d: _Dir, name: str, pkg: Any) -> str:
    """Write the package into a fresh private-name directory of ``addons/`` (every component created with
    ``O_EXCL|O_NOFOLLOW``; its names passed 8.1) and read it back: the digest must equal the grant's."""
    tmp = os.path.basename(tempfile.mkdtemp(prefix=f".{name}.new-", dir=d.path))
    opened: list[int] = []
    try:
        os.chmod(tmp, 0o755, dir_fd=d.fd, follow_symlinks=False)
        made = {"": os.open(tmp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=d.fd)}
        opened.append(made[""])
        for rel in sorted(pkg.files):
            parent = ""
            for part in rel.split("/")[:-1]:
                here = f"{parent}/{part}" if parent else part
                if here not in made:
                    os.mkdir(part, 0o755, dir_fd=made[parent])
                    made[here] = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=made[parent])
                    opened.append(made[here])
                parent = here
            out = os.open(
                rel.rsplit("/", 1)[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=made[parent]
            )
            with os.fdopen(out, "wb") as stream:
                stream.write(pkg.files[rel])
    except BaseException:
        for f in opened:
            os.close(f)
        _rmtree(d, tmp)
        raise
    for f in opened:
        os.close(f)
    if _digest_of(d, tmp) != pkg.digest:
        _rmtree(d, tmp)
        raise PackageError("the written package does not read back as the granted bytes")
    return tmp


def _rmtree(d: _Dir, name: str) -> None:
    """Remove ``name`` below the directory: a link or file is unlinked, never followed; a directory with its tree."""
    try:
        st = os.stat(name, dir_fd=d.fd, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode):
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=d.fd)
        return
    shutil.rmtree(name, ignore_errors=True, dir_fd=d.fd)


def _sweep(d: _Dir, name: str) -> None:
    for kind in ("new", "old"):
        for n in _siblings(d, name, kind):
            _rmtree(d, n)


# ------------------------------------------------------------------------------------------------ recovery at start
def recover(service: WorkspaceService) -> None:
    """At start, after the human requests were rolled forward: for each stage file, if the latest ``addon.granted``
    of its name has the stage's digest, finish the swap and the records (idempotent); else delete the
    stage when it expired."""
    files = _files(service)
    now = service.clock()
    for doc in files.stages():
        granted = _model_addon(service, doc["name"])
        if granted is not None and granted.package_sha256 == doc["digest"]:
            ev = {"name": doc["name"], "package_sha256": doc["digest"]}
            try:
                _converge(service, doc["stage"], ev)
            except Exception:  # noqa: BLE001
                LOG.exception("market install: recovery of %s failed", doc["name"])
        elif now > doc.get("expires_at", 0):
            files.drop_stage(doc["stage"])
    sweep_credentials(service)  # V3 G6 C4: idempotent at start
    root = _store_root(service)
    if (root / install.ADDONS_DIR).is_dir():
        with _addons_dir(root / install.ADDONS_DIR) as d:
            for n in os.listdir(d.fd):
                if n.startswith(".") and (".new-" in n or ".old-" in n):
                    name = n[1:].split(".", 1)[0]
                    g = _model_addon(service, name)
                    if g is not None and _digest_of(d, name) == g.package_sha256:
                        _rmtree(d, n)  # debris of a finished swap; an unfinished one is handled above
    addonenv.recover(service)  # then the debris of a crashed environment build


# ------------------------------------------------------------------------------------------------ installed, uninstall
def _installed(service: WorkspaceService) -> dict[str, Any]:
    from . import marketops

    try:  # the run-time view, from the pinned snapshot without network (spec 9)
        pol: Any = hubpolicy.stored(service)
    except MarketError:
        pol = hubpolicy.UNREADABLE
    files = _files(service)
    installs = files.installs()
    rows = []
    for name in sorted(installs[ADDONS]):
        rec = installs[ADDONS][name]
        model = _model_addon(service, name)
        state = state_of(service, name)
        avail = None
        try:
            cls, keys = market_class(service, rec["origin"], pol)
            raw = files.read_cache(rec["origin"])
            v = verdict(raw, rec["origin"], cls, keys, service.clock()) if raw is not None else None
        except MarketError:
            v = None
        if v is not None:
            e = next((x for x in v.doc[ADDONS] if x["id"] == name), None)
            revoked = f"{name}@{rec['version']}" in v.doc.get("revoked", [])
            if e is not None and (mi.semver(e["version"]) > mi.semver(rec["version"]) or revoked):
                model_caps = set(model.capabilities) if model is not None else set()
                avail = {
                    "version": e["version"], "package_sha256": e["package_sha256"],
                    "capabilities_added": sorted(set(e["capabilities"]) - model_caps), "revoked": revoked,
                    "stale": v.stale,
                }  # fmt: skip
        key = rec["publisher_key"]
        rows.append(
            {
                "name": name,
                "version": rec["version"],
                "package_sha256": rec["package_sha256"],
                "origin": rec["origin"],
                "commit": rec["commit"],
                "installed_at": rec["installed_at"],
                "state": state,
                "publisher": None if key is None else marketops.fingerprint(key),
                "available": avail,
                "env": addonenv.env_row(service, name, model),
                "paused": hubpolicy.paused(service, pol, rec, model, name),
            }  # fmt: skip
        )
    return {ADDONS: rows, "hub": hubpolicy.view(pol)}


def _uninstall(service: WorkspaceService, name: str) -> dict[str, Any]:
    files = _files(service)
    store = service.store
    with store.locked():
        store.refresh()
        with files.commit_installs() as write:
            cur = files.installs()
            if name not in cur[ADDONS]:
                raise MarketError("market.not_installed")
            model = _model_addon(service, name)
            if model is not None and model.enabled and not model.purged:
                raise MarketError("market.still_enabled")
            root = _store_root(service)
            if (root / install.ADDONS_DIR).is_dir():
                with _addons_dir(root / install.ADDONS_DIR) as d:
                    _rmtree(d, name)
                    _sweep(d, name)
            addonenv.remove(service, name)  # its dependency environment goes with it
            del cur[ADDONS][name]  # the rollback floor ("seen") and the publisher key stay
            write(cur)
    credential.remove(service.state_dir, service.workspace_id, name)  # V3 G6 C4: the key goes with the package
    return {"name": name, "removed": True}
