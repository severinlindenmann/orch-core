"""Test-only launcher for the real-host click tests: orch's own CLI or the real click signer (`orch.click`), with ONE
seam replaced: `orch.custody.convenience.open_store` returns the 0600 file store of the disposable state instead of the
macOS login keychain. That function is the documented test seam (core: "Tests replace this function"); nothing else
differs, and the keychain is never opened.
Usage: `python -I click_child.py signer --fd N`, `... cli ARGS` or `... dashboard serve`."""

import os
import sys
from pathlib import Path


def _refuse_unless_disposable() -> None:
    """The file store must never write under the person's real state: an explicit ORCH_STATE_DIR with the kit's
    `.orch-dev` marker, not under ~/.config/orch."""
    state = os.environ.get("ORCH_STATE_DIR", "")
    import pwd

    # The real home comes from the account database, not $HOME (the kit overrides HOME with a scratch directory).
    real = (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "orch").resolve()
    path = Path(state).resolve() if state else None
    if not path or path == real or real in path.parents or not (path / ".orch-dev").exists():
        sys.stderr.write("click_child.py: needs an explicit disposable ORCH_STATE_DIR with a .orch-dev marker\n")
        raise SystemExit(2)


def _use_file_store() -> None:
    from orch.custody import convenience

    def file_store(state_dir):
        return convenience.FileStore(convenience.person_root(state_dir))

    convenience.open_store = file_store


if __name__ == "__main__":
    _refuse_unless_disposable()
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "dashboard":
        # The dashboard server itself, with ONE seam: the signer child it starts is this launcher's `signer` mode (file
        # custody), not `python -I -m orch.click` (macOS keychain). For the e2e specs on a developer's Mac.
        import socket
        import subprocess

        from orch.click import spawn
        from orch.click.client import ClickClient

        # Test-only. The dashboard server process loads NO custody here (only the signer child it spawns, running this
        # same file in `signer` mode, swaps the store); the invariant "no custody in the HTTP process" is still
        # covered by the real-host pytest (tests/test_realhost_click.py).
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
    if mode not in ("signer", "cli"):
        sys.stderr.write("click_child.py: mode is signer, cli or dashboard\n")
        raise SystemExit(2)
    if mode in ("signer", "cli"):
        _use_file_store()  # only the processes that need custody load it; the dashboard server does not
    if mode == "signer":
        from orch.click.server import main as signer_main

        sys.exit(signer_main(rest))
    from orch.cli.main import main as cli_main  # mode == "cli", with the file store

    sys.exit(cli_main(rest))
