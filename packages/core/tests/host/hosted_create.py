"""A test-only way to make a workspace through a running host (V3-D38: ``orch setup`` is the only product path).

``create_hosted`` is what ``orch setup``'s genesis step does without the setup page: a generated passphrase and recovery
code, the library's hosted bootstrap loop (``host.wsk_create`` + signed genesis + ``host.bootstrap``), then the
instruction files. The terminal and the device key's backend are the tests' own fakes (``wi.TERMINAL``,
``wi.open_backend``)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orch import layout as layout_mod
from orch.custody.strength import generate_passphrase
from orch.identity import format_code_numbered, generate_recovery_code
from orch.instructions import write_workspace_files
from orch.ops import workspace_init as wi
from orch.ops.base import Context, Result
from orch.ops.runtime import state_dir


def create_hosted(ctx: Context, args: dict[str, Any]) -> Result:
    """Create ``args["prefix"]`` in the context's directory through the host that answers on its state directory."""
    root = Path(ctx.workspace._cwd if ctx.workspace is not None and ctx.workspace._cwd else os.getcwd())
    sd = state_dir(ctx.env, ctx.caps.files)
    prefix = args["prefix"]
    owner_name = args.get("name") or ctx.env.get("USER") or "Owner"
    wi._empty_root(root)
    wi.check_reserved_prefix(root, sd, prefix, owner_name)
    st = wi.BootstrapState()
    passphrase = generate_passphrase()
    code = generate_recovery_code()
    # the fake terminal's record feeds the backend (the passphrase) and the tests' secret scans (the code)
    wi.TERMINAL.show(f"Your generated passphrase:\n\n    {passphrase}\n")
    wi.TERMINAL.show(f"shown now, once.\n\n{format_code_numbered(code)}\n")
    try:
        wi._hosted_bootstrap(
            ctx, sd, root, prefix, owner_name, passphrase, code, st, workspace_name=args.get("workspace_name")
        )
    except KeyboardInterrupt:  # Ctrl-C: as `orch setup` does, keep the key of a bootstrap whose outcome is unknown
        wi._drop_unless_pending(sd, st.wid, st.pending)
        if st.pending is not None:
            raise wi._unavailable("setup was interrupted during host.bootstrap", st.pending) from None
        raise wi._refuse("stop", "cancelled; nothing was created") from None
    except BaseException:
        wi._drop_unless_pending(sd, st.wid, st.pending)
        raise
    finally:
        wi._close(st.client)
    write_workspace_files(root, store=layout_mod.store_of(root))
    return Result(
        data={"prefix": prefix, "workspace_id": st.wid},
        seq=int((st.result or {}).get("seq", 1)),
        lines=[f"ok init {prefix} seq=1"],
    )
