"""Mint a webhook doorbell address for a follow-up (#717). Nothing is stored here: the address starts working when a
follow-up is set with ``--on webhook:<id>`` (the ledger then keeps only the hash of the id)."""

from orch.host.hubhooks import TooManyHooks
from orch.model import followup_events as fe
from orch.ops._dsl import REF, STR, S, err, obj, operation
from orch.ops.base import Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call

SOURCES = ("github", "generic", "databricks")
HUB_HINT = {
    "github": "GitHub: Settings > Webhooks > Add webhook: Payload URL = url, Content type = application/json, "
    "Secret = secret; pick only the events you need (checks or workflow runs), not push; "
    "GitHub's first ping is recognised and does not use the ring",
    "databricks": "Databricks: Settings > Notifications > Add destination > Webhook: URL = url, "
    "username = orch, password = secret; then add it to the job's notifications; "
    '"Send test" cannot be told from a real run and uses the ring',
    "generic": 'a script: curl -X POST url -H "X-Orch-Signature: t=$T,v1=$SIG" where T is the unix time and SIG is '
    'HMAC-SHA256 with secret over "$T." followed by the body',
}
HINT = "this host does not receive webhooks: an owner runs: orch host webhooks on, then orch host restart"


def handle(ctx, args):
    c = Call.of(ctx, "webhook.new")
    view = c.resolve(args["ref"], live_only=True)
    label = c.text(args["label"], one_line=True, limit=200, what="label")
    hub = c.ctx.caps.hub_hooks
    if hub is not None:  # a hub with a public inbox: the address lives there (#1285); else the direct receiver below
        source = args.get("source", "generic")
        token, secret = fe.new_hub_token(), fe.new_hub_secret()
        try:
            hub_base = hub(token=token, secret=secret, source=source, followup=view.key)
        except TooManyHooks:
            raise OrchError("followup.too_many_hooks") from None
        except Exception:  # noqa: BLE001 - no hub, inbox off, link away, or hook limit: try the direct receiver
            hub_base = None
        if hub_base:
            url = f"{hub_base}{token}"
            lines = [
                f"url: {url}",
                f"secret: {secret}",
                f"shown once: {HUB_HINT[source]}",
                "a ring before the follow-up exists (a test ping, Send test) is ignored: "
                "set the follow-up first, test after",
                f"then: orch follow-up {view.key} --on webhook:{token} --in 3d --brief-file brief.md",
                "a ring only wakes the follow-up: nothing a request carries is stored or passed on",
            ]
            return Result(data={"id": token, "url": url, "label": label, "secret": secret}, key=view.key, lines=lines)
    base = (c.ctx.caps.env.get("ORCH_HOOK_BASE") or "").rstrip("/")
    if not base:
        raise OrchError("followup.no_receiver", HINT)
    hook = fe.new_hook_id()
    url = f"{base}/h/{hook}"
    lines = [
        f"url: {url}",
        "shown once: put it where the outside job ends, e.g. curl -X POST <url> as the last step",
        f"then: orch follow-up {view.key} --on webhook:{hook} --in 3d --brief-file brief.md",
        "a ring only wakes the follow-up: nothing a request carries is stored or passed on",
    ]
    return Result(data={"id": hook, "url": url, "label": label}, key=view.key, lines=lines)


OP = operation(
    "webhook.new",
    "Lifecycle",
    "Make a doorbell address that wakes one follow-up when an outside job calls it.",
    who="read",
    props={
        "ref": REF(),
        "label": S("what it is for", maxLength=200),
        "source": S("who will call it, with a hub: github, generic (a script) or databricks", enum=list(SOURCES)),
    },
    required=("label",),
    positional=("ref",),
    text="ok {key} webhook {label}",
    data=obj({"id": STR, "url": STR, "label": STR, "secret": STR}, optional=("secret",)),
    errors=(err("followup.no_receiver"), err("followup.too_many_hooks"), err("not_found"), err("parse.text")),
    handler=handle,
    addon="schedules",
)
