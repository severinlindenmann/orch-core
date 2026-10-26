"""orch v2 core."""

__version__ = "2.0.0.dev0"

import os as _os

if (
    _os.name == "nt"
):  # POSIX imports nothing here: the root-trusted setup tree (setup/oslauncher.TRUSTED_FILES) has only __init__
    from . import _winflags

    _winflags.install()
