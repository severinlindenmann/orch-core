"""``python -P -m orch_dashboard.remote_client``: the remote client on this computer (R5 design section 1).

Starts the home page on its own ``127.0.0.1`` port, opens the person's browser at it with a single-use launch code and
keeps running until nothing has used it for 30 minutes (no open workspace, no request). One per OS user is the aim; a
second start simply makes a second home page (no shared state beyond the client's files).

The presence key: on a Mac the signed helper (``<client state>/helper/OrchRemoteKey.app/...``) when it is installed, a
security key (FIDO2 over USB) otherwise. There is no software fallback."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

IDLE_EXIT_S = 30 * 60
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
HELPER = Path("helper/OrchRemoteKey.app/Contents/MacOS/orch-remote-key")


def _presence(root: Path, store):  # noqa: ANN001
    from orch.remoteclient import Fido2Backend, HidAuthenticator, SecureEnclaveBackend

    if sys.platform == "darwin" and (root / HELPER).is_file():
        be = SecureEnclaveBackend(root / HELPER, store=root / "helper-keys")
        if be.available():
            return be
    return Fido2Backend(HidAuthenticator(), store)


def main(
    argv: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    sleep: float = 5.0,
) -> int:
    env = os.environ if env is None else env
    out, err = out or sys.stdout, err or sys.stderr
    if any(env.get(n) for n in AGENT_ENV):
        err.write("orch remote open: this is a person's; an agent never opens it.\n")
        return 3
    from orch.remoteclient import Client, Store, default_root

    from . import Controller, GatedPresence, StaticSite, installed_static_dir

    root = default_root(env)
    store = Store(root)
    gate = GatedPresence(_presence(root, store))
    client = Client(store, presence=gate)
    try:
        static = StaticSite.load(installed_static_dir())
    except (OSError, ValueError):
        err.write("orch remote open: the dashboard build is not installed.\n")
        return 1
    if static.page("home") is None:
        err.write("orch remote open: this dashboard build has no remote client page. Update orch.\n")
        return 1
    ctl = Controller(client, static, gate=gate)
    url = ctl.start_home()
    out.write(f"orch remote client: {url.split('#')[0]}\n")
    out.flush()
    ctl.open_home()  # the browser gets a private redirect file's path; the launch code is never in its argv
    try:
        idle_since = time.monotonic()
        while True:
            time.sleep(sleep)
            assert ctl.home is not None
            if ctl._open or time.monotonic() - ctl.home.last_seen < IDLE_EXIT_S:  # noqa: SLF001
                idle_since = time.monotonic()
            if time.monotonic() - idle_since > IDLE_EXIT_S:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        ctl.stop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
