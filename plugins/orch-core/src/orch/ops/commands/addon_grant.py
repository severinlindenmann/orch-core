"""orch addon grant: grant an addon its capabilities (owner only); also enables it"""

import os
from pathlib import Path
from typing import Any

from orch.addons import install
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def _install_builtin(h: Human, root: Any, name: str) -> None:
    """An add-on that ships inside orch-core and was never installed here (left off at setup, now turned on from the
    dashboard): put its own manifest in place, the same bytes setup writes. Only a missing package is filled in, never
    one that exists; the signed grant covers the digest of what was written, so the person signs exactly this package.

    Nothing is written for a ``--dry-run`` or for anyone but a workspace owner (``role.denied``, the model's rule for
    ``addon.granted``). A failed write removes the empty directory it made and says why."""
    from orch.addons import builtin

    dry = h.ctx.dry_run or (h.ctx.prepare is not None and h.ctx.prepare.dry_run)
    if name not in builtin.NAMES or dry:
        return
    member = h.store.state.workspace.members.get(h.person)
    if member is None or member.role != "owner":
        raise OrchError("role.denied", "turning an addon on is for workspace owners")
    try:
        builtin.install_missing(str(root), name)
    except (OSError, ValueError) as e:
        raise OrchError("invalid.input", f"cannot install addons/{name}: {type(e).__name__}") from None


def _market_files(h: Human) -> Any:
    """The host's records: through the capture under the host, from the state directory in direct mode."""
    from orch.host.marketstate import MarketFiles

    if h.ctx.prepare is not None and h.ctx.prepare.market_files is not None:
        return h.ctx.prepare.market_files
    return MarketFiles(h.call.ws.state_dir, h.workspace_id, os.getuid())


def _market_refusal(exc: Exception) -> OrchError:
    from orch.ops.errors import ERRORS

    code = getattr(exc, "code", "market.staged_changed")
    return OrchError(code, ERRORS[code].message if code in ERRORS else code)


def _staged(h: Human, ctx: Context, name: str, stage: str) -> tuple[Any, Any, Any]:
    """``addon.grant {staged}`` (V3 G3): the package comes from the stage file the host wrote at ``market.stage`` (read
    once, digest-checked), never from ``addons/``; the review and the event are built from those bytes exactly as for a
    folder. The marketplace block travels as the host's word about the source (``Capture.market``, not signed)."""
    from orch.host import marketinstall, marketops
    from orch.market import MarketError

    if ctx.prepare is None:
        raise OrchError("invalid.input", "a staged package is signed through the host: orch addon install NAME")
    try:
        doc, package, manifest = marketinstall.staged_package(_market_files(h), name, stage, ctx.now())
    except MarketError as e:
        raise _market_refusal(e) from None
    key = doc["publisher_key"]
    ctx.prepare.staged = {"stage": stage, "name": name}
    ctx.prepare.market = {
        "url": doc["origin"], "class": doc["class"], "commit": doc["commit"], "source_note": marketinstall.SOURCE_NOTE,
        "publisher": {"fingerprint": None if key is None else marketops.fingerprint(key), "tofu": doc["tofu"]},
        "index_version": doc["index_version"], "stale": bool(doc["stale"]), "summary": doc["summary"],
        "replaces": doc["prev_digest"],
    }  # fmt: skip
    return doc, package, manifest


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The grant is built from the package bytes read once (ticket-format 8.1): exactly the manifest's capabilities and
    its declared fields, sections and artifact kinds. The owner reads a summary on the terminal and types the addon's
    name; the signature then covers the digest, the capabilities and the declarations, which is all replay ever
    uses."""
    h = Human(ctx, "addon.grant")
    h.who()
    name = args["name"]
    h.call.ws.require_root()
    from orch.addons import settings_needs

    refusal = settings_needs.grant_refusal(h.store.state.workspace.settings, name)  # AE4: never on without settings
    if refusal is not None:
        raise OrchError("invalid.input", refusal, hint="set it up in Settings, Add-ons, then turn it on")
    root = h.store.layout.store  # addons/ lives in the store
    stage = args.get("staged")
    if stage is not None:
        _, package, manifest = _staged(h, ctx, name, stage)
    else:
        try:
            directory = install.package_dir(root, name)
        except PackageError as e:
            raise OrchError("invalid.input", str(e)) from None
        if not Path(directory).exists():
            _install_builtin(h, root, name)
        if not Path(directory).is_dir():
            raise OrchError("not_found", f"no package at addons/{name}", hint=f"put the addon in addons/{name}/")
        try:
            package, manifest = install.read_installed(root, name)
        except (PackageError, ManifestError) as e:
            raise OrchError("invalid.input", f"addons/{name}: {e}") from None
    event = install.grant_event(manifest, package)
    if stage is None:  # C1: a folder is signed only when it is built in or the bytes the install record holds
        from orch.host import marketinstall
        from orch.market import MarketError

        try:
            marketinstall.gate_plain(_market_files(h), event)
        except MarketError as e:
            raise _market_refusal(e) from None
    view = h.store.state.workspace.addons
    registry = install.load_registry(root, view)
    clash = registry.heading_conflicts(manifest)
    if clash:
        raise OrchError("invalid.input", f"section heading {clash[0]!r} is already used by another granted addon")
    previous = install.as_addons(view).get(name)
    caps = ",".join(event["capabilities"]) or "none"
    review = install.review_lines(manifest, package, event, previous)
    done = h.run(event, "workspace", f"grant addon {name} {manifest.version} capabilities {caps}", review=review)
    lines = [
        f"version {manifest.version}",
        f"package {package.digest}",
        f"capabilities {caps}",
        f"declares {len(event['fields'])} fields, {len(event['sections'])} sections, "
        f"{len(event['artifact_kinds'])} artifact kinds",
    ]
    return h.workspace_result(done, {"name": name}, "orch addon list", lines)


OP = operation(
    "addon.grant",
    "Admin",
    "Grant an addon its capabilities (owner only); also enables it.",
    who="human",
    props={
        "name": S("addon name", **{"x-metavar": "NAME"}),
        "staged": S(
            "the stage id that `orch addon install` or `update` made (the package comes from it, not from addons/)",
            **{"x-metavar": "STAGE", "pattern": "^st_[0-9a-f]{32}$"},
        ),
    },
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.granted",),
    text="ok addon.granted {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(
        err("role.denied"),
        err("not_found"),
        err("market.not_installed"),
        err("market.staged_changed"),
        err("market.manifest_mismatch"),
        err("market.origin_changed"),
        err("market.version_reused"),
        err("market.rollback"),
        err("market.publisher_key_changed"),
    ),
    handler=handle,
)
