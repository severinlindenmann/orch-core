"""``safe_read`` (host-mode ``check`` -> ``stale_findings``) opens through ``fsio.open_regular`` (#564): a FIFO in
place of an instructions file is refused at once, never read (it would block)."""

from __future__ import annotations

import os
import signal

import pytest

from orch.instructions.harness import UnsafePath, safe_read


def test_a_fifo_is_refused_without_blocking(tmp_path, monkeypatch):
    from orch.instructions import harness

    # the chain check saw a regular file; the FIFO lands right after it (the race the open must close)
    monkeypatch.setattr(harness, "_check_chain", lambda root, rel: root / rel)

    def boom(*_):
        raise TimeoutError("blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    try:
        os.mkfifo(tmp_path / "AGENTS.md")
        with pytest.raises(UnsafePath):
            safe_read(tmp_path, "AGENTS.md")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
