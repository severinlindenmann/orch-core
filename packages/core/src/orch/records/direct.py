"""Direct mode for the records tool (D-P2-38): a workspace no host serves.

The tool takes the snapshot itself under the store lock and applies the same rules as the host ops, on the same files
(:func:`orch.host.records.run_op` with a direct :class:`~orch.host.records.Ctx`): the manifest, the export check
against the local reader rules (the convenience flag in the state dir and convenience events in the history), the
private-remote mark, the note and the status in ``<state>/hosts/<ws>/records.json``.

* The store is opened read-only (no workspace key) with :class:`~orch.host.ownership.DirectGuard`: every store
  transaction holds the ownership lock shared and answers ``host.required`` once a host serves the workspace, so the
  tool never works underneath a host. The host's ``<root>/.state/host-served`` marker is checked too, so another
  state directory does not hide a serving host.
* POSIX only: on Windows the tool refuses (``records.unsupported``) until reparse-point-safe resolution exists.
* No role check: in direct mode the tool runs as the person who owns these files and could copy them by hand; the
  export check still applies.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import RecordsError

__all__ = ["DirectLink"]


class _Holder:
    records_state: dict[str, Any] | None = None


class DirectLink:
    """``link(op, args)`` for a workspace in direct mode."""

    def __init__(
        self,
        root: Path,
        workspace_id: str,
        state: Path,
        *,
        ident: tuple[int, int] | None = None,
        config: bytes | None = None,
        store_pin: Any = None,
    ) -> None:
        if os.name == "nt":
            raise RecordsError("records.unsupported", "direct-mode records do not run on Windows yet (D-P2-38)")
        self.root, self.workspace_id, self.state = Path(root), workspace_id, Path(state)
        #: the root's identity when it was discovered here: checked before the store opens below it
        self.ident = ident
        self.store_pin = store_pin
        #: the config.json bytes discovery read: the store's open-time check uses them, never a re-read by path
        self.config = config
        self._store: Any = None
        self._holder = _Holder()

    def served(self) -> bool:
        """A host serves this workspace: its marker under the root (written by the host whatever state directory this
        tool was given; Opus #433 finding 3), or the registration in this state directory (the guard)."""
        from orch.host.service import is_served

        return is_served(self.root)  # either layout; a layout it cannot place counts as served (fail closed)

    def _open(self) -> Any:
        if self.served():
            raise RecordsError(
                "host.required",
                "a host serves this workspace; direct-mode records are off",
                hint="run orch records with the state directory of that host (the default), or orch host unregister",
                exit_=3,
            )
        if self._store is None:
            from orch.discovery import same_root
            from orch.host.ownership import DirectGuard
            from orch.ops.errors import OrchError
            from orch.store import Store, StoreError

            if self.ident is not None and not same_root(self.root, self.ident):
                raise RecordsError(
                    "records.unsupported", f"{self.root} is no longer the workspace directory that was found", exit_=3
                )
            try:
                self._store = Store.open(
                    self.root,
                    expected_workspace_id=self.workspace_id,
                    host=None,
                    host_state_dir=self.state,
                    load="lazy",
                    guard=DirectGuard(self.state, self.workspace_id),
                    config=self.config,
                    ident=self.ident,
                    store_pin=self.store_pin,
                )
            except StoreError as exc:
                raise RecordsError(exc.code, exc.detail or exc.code, exit_=3) from None
            except OrchError as exc:
                raise RecordsError(exc.code, str(exc.message), hint=exc.hint, exit_=3) from None
        return self._store

    def __call__(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        from orch.host import records as host_records
        from orch.host.errors import HostError
        from orch.ops.errors import OrchError
        from orch.store import StoreError

        try:
            host_records._validate(op, dict(args))
            ctx = host_records.Ctx(
                store=self._open(),
                state_dir=self.state,
                workspace_id=self.workspace_id,
                host_uid=os.getuid(),
                clock=time.time,
                principal=f"direct:{os.getuid()}",
                person=None,
                sees=lambda _view: True,
                holder=self._holder,
            )
            return host_records.run_op(ctx, op, dict(args))
        except HostError as exc:
            raise RecordsError(exc.code, exc.message, hint=exc.hint, exit_=exc.exit) from None
        except OrchError as exc:  # host.required from the guard: a host serves the workspace now
            raise RecordsError(exc.code, str(exc.message), hint=exc.hint, exit_=3) from None
        except StoreError as exc:
            raise RecordsError(exc.code, exc.detail or exc.code, exit_=3) from None

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
