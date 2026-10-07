from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from orch.config.load import find_home, load_config
from orch.core.constants import STATUSES


@dataclass
class Workspace:
    home: Path
    config: dict
    _addons: object | None = field(default=None, repr=False, compare=False)
    # Concurrent first requests in `orch serve` must import the addons once, not once per worker thread.
    addons_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def open(cls, start: Path | None = None, *, use_env: bool = True) -> "Workspace":
        home = find_home(start, use_env=use_env)
        ws = cls(home=home, config=load_config(home))
        ws.ensure_layout()
        return ws

    @property
    def root(self) -> Path:
        return self.home.parent

    @property
    def tickets_dir(self) -> Path:
        return self.home / "tickets"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def temporary_dir(self) -> Path:
        return self.home / "temporary"

    @property
    def static_dir(self) -> Path:
        return self.home / "static"

    @property
    def share_dir(self) -> Path:
        return self.home / "share"

    @property
    def state_dir(self) -> Path:
        return self.home / ".state"

    def status_dir(self, status: str) -> Path:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        return self.tickets_dir / status

    def ensure_layout(self) -> None:
        for status in STATUSES:
            self.status_dir(status).mkdir(parents=True, exist_ok=True)
        for d in (self.artifacts_dir, self.temporary_dir, self.static_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def addons(self):
        if self._addons is None:
            from orch.addons.loader import AddonRegistry
            with self.addons_lock:
                if self._addons is None:
                    self._addons = AddonRegistry.load(self)
        return self._addons
