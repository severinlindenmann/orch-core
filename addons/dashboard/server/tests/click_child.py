"""Test-only launcher for the real-host click tests: orch's own CLI or the real click signer (`orch.click`), with ONE
seam replaced: `orch.custody.convenience.open_store` returns the 0600 file store of the disposable state instead of the
macOS login keychain. That function is the documented test seam (core: "Tests replace this function"); nothing else
differs, and the keychain is never opened. Usage: `python -I click_child.py signer --fd N` or `... cli ARGS`."""

import sys

from orch.custody import convenience


def _file_store(state_dir):
    return convenience.FileStore(convenience.person_root(state_dir))


convenience.open_store = _file_store

if __name__ == "__main__":
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "signer":
        from orch.click.server import main as signer_main

        sys.exit(signer_main(rest))
    from orch.cli.main import main as cli_main

    sys.exit(cli_main(rest))
