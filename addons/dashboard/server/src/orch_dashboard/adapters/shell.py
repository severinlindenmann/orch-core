"""Shell projections: workspaces, members and identity. Unavailable fields are null, never guessed."""

import re
from typing import Literal

from pydantic import BaseModel

from ..text import hidden
from .addons import runner

TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

DEVICE = re.compile(r"d_[0-9a-f]{32}")


class Member(BaseModel):
    person: str
    name: str
    role: Literal["owner", "maintainer", "member", "viewer"]
    devices: int
    # The host's name had invisible characters: it is not shown (nor repaired); the person id stands in for it.
    name_hidden: bool = False


class Workspace(BaseModel):
    id: str
    prefix: str
    name: str
    members: list[Member] = []
    needs_you: int | None = None
    host_state: str = "ok"
    # Why the host cannot serve this workspace, in the host's own plain words (a store from an older version: what to
    # delete). Only for a workspace whose host_state is not ok; absent when the host said nothing usable.
    host_message: str | None = None
    features: list[str] = []
    # What the signer says about this workspace (custody, enabled, available, problem); null: no signer answered.
    click: dict | None = None
    runner: dict | None = None
    # V3 C11: the host's signing mode (`full`/`unknown`: approvals go through the hub); null: not known.
    signing: str | None = None
    # The signed workspace icon (`settings.icon`): an icon and a colour of core's closed lists, or null (none signed:
    # the page shows the default from the id). `icon_known`: the host reports the setting (an older host does not).
    icon: dict | None = None
    icon_known: bool = False


def workspace_icon(settings) -> dict | None:
    """The host's `icon` from `workspace.settings`: only a plain pair of core's closed lists, else ``None`` (the
    default shows; nothing the host sent outside the lists reaches the page)."""
    from orch.wsicons import COLOUR_LABELS, ICON_LABELS

    value = settings.get("icon") if isinstance(settings, dict) else None
    if (
        isinstance(value, dict)
        and set(value) == {"icon", "colour"}
        and value["icon"] in ICON_LABELS
        and value["colour"] in COLOUR_LABELS
    ):
        return {"icon": value["icon"], "colour": value["colour"]}
    return None


_PREFIX = re.compile(r"[A-Z0-9]{1,16}")
_CAUSE = re.compile(r"^(?:the workspace cannot be served: )?(?:[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+: )?")


def pinned_prefix(row) -> str:
    """The prefix of a workspace the host cannot serve, from the trusted registry row (the host cannot say it). Empty
    when the row has none that is plain."""
    prefix = row.get("prefix") if isinstance(row, dict) else None
    return prefix if isinstance(prefix, str) and _PREFIX.fullmatch(prefix) else ""


def unavailable_reason(error) -> str | None:
    """The host's reason a workspace cannot be served, without the error codes in front of it; ``None`` when it is
    missing, too long or has invisible characters (never repaired, never shown)."""
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or error.get("code") != "workspace.unavailable":
        return None
    text = _CAUSE.sub("", message, count=1).strip()
    if not text or len(text) > 600 or hidden(text):
        return None
    return text


def click_card(row):
    """The signer's per-workspace status as the card needs it. Only these fields; nothing else is passed on."""
    if not isinstance(row, dict):
        return None
    out = {
        "custody": row.get("custody") if row.get("custody") in ("convenience", "passphrase", "none") else "none",
        "enabled": row.get("enabled") is True,
        "available": row.get("available") is True,
    }
    if isinstance(row.get("problem"), str):
        out["problem"] = row["problem"][:200]
    hours = row.get("idle_hours")  # C17: it switched itself off after this many hours without use
    if type(hours) is int and 1 <= hours <= 168:
        out["idle_hours"] = hours
    ack = row.get("risk_ack")  # B6: when the one-click risk notice was acknowledged at enable time, else null
    out["risk_ack"] = ack if isinstance(ack, str) and TIME.fullmatch(ack) else None
    if isinstance(row.get("device"), str) and DEVICE.fullmatch(row["device"]):
        out["device"] = row["device"]  # the key the switch would move (shown before the person types the passphrase)
    return out


class Me(BaseModel):
    person: str
    name: str
    role: str
    tier: str
    workspace: str
    runner: dict | None = None


def safe_name(m) -> tuple[str, bool]:
    """A member's name, or their person id when the host's name can't be shown safely (invisible characters could
    make it pass for another person's). The name is never repaired; one such member doesn't hide the workspace."""
    if isinstance(m.get("name"), str) and not hidden(m["name"]):
        return m["name"], False
    return m["person"], True


def members(data):
    out = []
    for m in data["members"]:
        name, name_hidden = safe_name(m)
        out.append(
            Member(person=m["person"], name=name, role=m["role"], devices=len(m["devices"]), name_hidden=name_hidden)
        )
    return out


def me(identity, roster, ws, *, runner_supported=False):
    member = next(m for m in members(roster) if m.person == identity["person"])
    return Me(
        **member.model_dump(exclude={"devices"}),
        tier=identity["tier"],
        workspace=ws,
        runner=runner(identity, runner_supported),
    )


SANDBOX_ID = re.compile(r"[0-9a-f]{8}")
WORKSPACE_ID = re.compile(r"[0-9a-f]{32}")
ALARM_CODES = ("sandbox_workspace_registered", "dev_convenience_refused")
ALARMS_MAX = 16


def host_info(data):
    """The host-level dev facts of `host.list` (core D1, #582): `dev` (only a dev host has it) and `alarms` (only while
    there are any). Each field is checked and rebuilt; nothing else is passed on. A code this dashboard does not know
    stays as a code (`code` only, never text); a `workspace_id` that is not 32 lowercase hex is dropped."""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    dev = data.get("dev")
    if isinstance(dev, dict) and dev.get("sim_convenience") is True:
        out["dev"] = {"sim_convenience": True}
        sandbox = dev.get("sandbox")
        if isinstance(sandbox, str) and SANDBOX_ID.fullmatch(sandbox):
            out["dev"]["sandbox"] = sandbox
    alarms = data.get("alarms")
    if isinstance(alarms, list):
        rows = []
        for alarm in alarms[:ALARMS_MAX]:
            code = alarm.get("code") if isinstance(alarm, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                continue
            row: dict = {"code": code}
            wid = alarm.get("workspace_id")
            if isinstance(wid, str) and WORKSPACE_ID.fullmatch(wid):
                row["workspace_id"] = wid
            rows.append(row)
        if rows:
            out["alarms"] = rows
    return out


ISOLATION_MAX = 20
ISOLATION_TEXT_MAX = 400


def isolation_rows(data):
    """The isolation problems of `host.status` (L13, person only): a list of `{what, fix}` strings, rebuilt row by row,
    at most 20 rows of 400 characters each. Anything else is dropped; a host that sends none gives an empty list."""
    rows = data.get("isolation") if isinstance(data, dict) else None
    out = []
    for row in rows[:ISOLATION_MAX] if isinstance(rows, list) else []:
        what = row.get("what") if isinstance(row, dict) else None
        fix = row.get("fix") if isinstance(row, dict) else None
        if isinstance(what, str) and isinstance(fix, str):
            out.append({"what": what[:ISOLATION_TEXT_MAX], "fix": fix[:ISOLATION_TEXT_MAX]})
    return out
