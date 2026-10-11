"""Run the host in this process and SIGKILL it at one crash point (tests only; not part of the package).

``python crashhost.py <point> <state> <run> [<pins dir>]``. The points patch one function so the process dies at an
exact step of a control write, the way ``kill -9`` would.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from pathlib import Path


def die(*_: object, **__: object) -> None:
    os.kill(os.getpid(), signal.SIGKILL)
    threading.Event().wait()  # SIGKILL to oneself may land a moment later: never run another line


def after(original):
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        die()
        return result

    return wrapped


def patch(point: str) -> None:
    from orch.host import intents, registry
    from orch.store import store

    if point == "register-before-entry":
        registry.Registry.add = die
    elif point in {"register-after-entry", "unregister-after-remove", "bootstrap-after-register"}:
        registry_complete = intents.IntentStore.complete

        def complete(self, intent, result):
            if intent.op.removeprefix("host.") in point:
                die()
            return registry_complete(self, intent, result)

        intents.IntentStore.complete = complete
    elif point == "unregister-before-remove":
        registry.Registry.remove = die
    elif point == "bootstrap-before-append":
        store.Store.append = die
    elif point == "bootstrap-after-append":
        store.Store.append = after(store.Store.append)
    elif point != "none":
        raise SystemExit(f"unknown crash point {point}")


def main() -> None:
    point, state, run = sys.argv[1:4]
    patch(point)
    from orch.host.pins import AgentPins
    from orch.host.service import Host

    pins = AgentPins(Path(sys.argv[4]), owner=os.getuid()) if len(sys.argv) > 4 else None
    host = Host(Path(state), Path(run), pins=pins)
    try:
        asyncio.run(host.run())
    except Exception as exc:  # noqa: BLE001 - report the code the way orch host run does
        print(getattr(exc, "code", type(exc).__name__), exc, file=sys.stderr)
        raise SystemExit(getattr(exc, "exit", 1)) from None


if __name__ == "__main__":
    main()
