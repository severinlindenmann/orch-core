"""Display-only version labels; never a compatibility or authorization gate."""

import re

from . import __version__

MIN_SUPPORTED_VERSION = "2.0.0.dev0"
#: the oldest phone app the Phones page does not flag "Update needed". Display only: no route ever refuses on it.
MIN_PHONE_VERSION = "0.1.0"


def version_state(v: str | None, hub_v: str | None, min_v: str | None) -> str:
    def parts(value):
        match = re.match(r"^(\d+(?:\.\d+)*)", value) if isinstance(value, str) else None
        if match is None:
            return None
        numbers = [int(n) for n in match[1].split(".")]
        while numbers and numbers[-1] == 0:
            numbers.pop()
        return tuple(numbers)

    current, hub, minimum = parts(v), parts(hub_v), parts(min_v)
    if current is None or hub is None or minimum is None:
        return "unknown"
    if current < minimum:
        return "unsupported"
    return "older" if current < hub else "newest"


def workspace_status(registry, ws):
    status = registry.status(ws)
    return {**status, "version_state": version_state(status["version"], __version__, MIN_SUPPORTED_VERSION)}
