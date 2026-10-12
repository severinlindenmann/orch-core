"""Test-only launcher for the real-host click tests: orch's own CLI or the real click signer (`orch.click`), with ONE
seam replaced: `orch.custody.convenience.open_store` returns the 0600 file store of the disposable state instead of the
macOS login keychain. That function is the documented test seam (core: "Tests replace this function"); nothing else
differs, and the keychain is never opened.
Usage: `python -I click_child.py signer --fd N`, `... cli ARGS` or `... dashboard serve`."""

import sys

from orch.custody import convenience


def _file_store(state_dir):
    return convenience.FileStore(convenience.person_root(state_dir))


convenience.open_store = _file_store

if __name__ == "__main__":
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "dashboard":
        # The dashboard server itself, with ONE seam: the signer child it starts is this launcher's `signer` mode (file
        # custody), not `python -I -m orch.click` (macOS keychain). For the e2e specs on a developer's Mac.
        import socket
        import subprocess

        from orch.click import spawn
        from orch.click.client import ClickClient

        def start(env=None):
            import os

            parent, child = socket.socketpair()
            n = child.fileno()
            proc = subprocess.Popen(
                [sys.executable, "-I", __file__, "signer", "--fd", str(n)],
                pass_fds=(n,),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                env=dict(os.environ if env is None else env),
            )
            child.close()
            return ClickClient(parent, proc)

        spawn.start = start
        from orch_dashboard.__main__ import main as dashboard_main

        sys.argv = ["orch_dashboard", *rest]
        sys.exit(dashboard_main())
    if mode == "signer":
        from orch.click.server import main as signer_main

        sys.exit(signer_main(rest))
    from orch.cli.main import main as cli_main

    sys.exit(cli_main(rest))
