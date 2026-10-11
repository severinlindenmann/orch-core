"""orch addon grant: grant an addon its capabilities (owner only); also enables it"""

from pathlib import Path
from typing import Any

from orch.addons import install
from orch.addons.manifest import ManifestError
from orch.addons.package import PackageError
from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """The grant is built from the package bytes read once (ticket-format 8.1): exactly the manifest's capabilities and
    its declared fields, sections and artifact kinds. The owner reads a summary on the terminal and types the addon's
    name; the signature then covers the digest, the capabilities and the declarations, which is all replay ever
    uses."""
    h = Human(ctx, "addon.grant")
    h.who()
    name = args["name"]
    root = h.call.ws.require_root()
    try:
        directory = install.package_dir(root, name)
    except PackageError as e:
        raise OrchError("invalid.input", str(e)) from None
    if not Path(directory).is_dir():
        raise OrchError("not_found", f"no package at addons/{name}", hint=f"put the addon in addons/{name}/")
    try:
        package, manifest = install.read_installed(root, name)
    except (PackageError, ManifestError) as e:
        raise OrchError("invalid.input", f"addons/{name}: {e}") from None
    event = install.grant_event(manifest, package)
    view = h.store.state.workspace.addons
    registry = install.load_registry(root, view)
    clash = registry.heading_conflicts(manifest)
    if clash:
        raise OrchError("invalid.input", f"section heading {clash[0]!r} is already used by another granted addon")
    previous = install.as_addons(view).get(name)
    caps = ",".join(event["capabilities"]) or "none"
    review = install.review_lines(manifest, package, event, previous)
    done = h.run(
        event, "workspace", f"grant addon {name} {manifest.version} capabilities {caps}", review=review, expect=name
    )
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
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.granted",),
    text="ok addon.granted {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle,
)
