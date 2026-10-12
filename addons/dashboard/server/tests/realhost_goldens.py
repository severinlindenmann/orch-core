"""What the adapters make of recorded (scrubbed) answers of a real host: shared by the recorder and the offline test."""

from pathlib import Path

from orch_dashboard import adapters

DIRECTORY = Path(__file__).parents[2] / "src/test/fixtures/real-host"


def adapt(raw: dict) -> dict:
    who = raw["whoami"]["person"]
    needs = raw["needs"]
    mine: dict[str, list] = {}
    for n in needs["items"]:
        if n.get("uid"):
            mine.setdefault(n["uid"], []).append(n)
    role = next(m["role"] for m in raw["member.list"]["members"] if m["person"] == who)
    return {
        "page": adapters.page(raw["ticket.query"], needs, who),
        "documents": {
            name: adapters.document(data, who, mine, ext=True) for name, data in sorted(raw["tickets"].items())
        },
        "events": {name: adapters.events(data) for name, data in sorted(raw["tickets"].items())},
        "settings": adapters.settings.settings(raw["workspace.settings"], [], False).model_dump(mode="json"),
        "roster": [m.model_dump(mode="json") for m in adapters.settings.roster(raw["member.list"])],
        "grants": [g.model_dump(mode="json") for g in adapters.settings.grants(raw["grant.list"], True)],
        "members": [m.model_dump(mode="json") for m in adapters.members(raw["member.list"])],
        "today": adapters.today(
            needs,
            raw["ticket.query"]["rows"],
            raw["whoami"]["workspace"],
            raw["ticket.query"].get("as_of") or "",
            who,
            role,
            [],
            [],
        ),
    }
