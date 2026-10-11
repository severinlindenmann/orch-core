"""orch addon purge: delete an addon's data"""

from typing import Any

from orch.ops._dsl import STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "addon.purge")
    h.who()
    name = args["name"]
    if name not in h.store.state.workspace.addons:
        raise OrchError("not_found", "no such addon was granted", hint="orch addon list")
    if h.store.state.workspace.addons[name]["purged"]:  # nothing left to remove: nothing to sign
        return Result(
            data={"name": name},
            seq=h.store.head_seq("workspace"),
            lines=["already purged: nothing signed"],
            hints=["orch addon list"],
        )
    h.store.load_all()  # the count below must see every ticket, not the ones loaded so far
    tickets = fields = sections = artifacts = 0
    for t in h.store.state.tickets.values():  # what the purge will remove from the derived state (events stay)
        data = t.fields["addons"].get(name)
        mine = [s for s in t.sections if s.startswith(name + ".")]
        arts = [x for x in t.artifacts if x.addon == name]
        if data or mine or arts:
            tickets += 1
        fields += len(data or {})
        sections += len(mine)
        artifacts += len(arts)
    done = h.run({"type": "addon.purged", "name": name}, "workspace", "purge addon " + name)
    verb = "removes" if h.ctx.dry_run else "removed"
    lines = [f"{verb} {fields} fields, {sections} sections, {artifacts} artifacts of {tickets} tickets (events stay)"]
    return h.workspace_result(done, {"name": name}, "orch addon list", lines)


OP = operation(
    "addon.purge",
    "Admin",
    "Delete an addon's data.",
    who="human",
    props={"name": S("addon name", **{"x-metavar": "NAME"})},
    required=("name",),
    positional=("name",),
    pre=("workspace_exists", "addon_exists", "role_allows", "user_presence"),
    emits=("addon.purged",),
    text="ok addon.purged {name} seq={seq}\nnext: {next}",
    data=obj({"name": STR}),
    errors=(err("role.denied"), err("not_found")),
    handler=handle,
)
