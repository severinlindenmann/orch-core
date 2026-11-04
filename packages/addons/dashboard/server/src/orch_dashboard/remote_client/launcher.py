"""Opening the person's browser without the launch code in any process argument.

A command line is readable by every user of the computer (``ps``). So the browser is given only the path of a small
redirect file in a private (0700) directory; the file (0600) holds the address with the single-use code and sends the
browser on. The file is deleted as soon as its code is redeemed, at its expiry, and at start-up (anything left over)."""

from __future__ import annotations

import html
import json
import os
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

__all__ = ["Launcher", "TTL_S"]

TTL_S = 60.0  # a launch code lives this long (origin.LAUNCH_TTL_S); so does its file


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise RuntimeError("the launch directory must be yours and private (mode 0700)")
    finally:
        os.close(fd)
    return path


class Launcher:
    def __init__(self, opener: Callable[[str], object], directory: Path | None = None, ttl: float = TTL_S) -> None:
        self.opener, self.ttl = opener, ttl
        self._own_tmp = directory is None
        self.dir = _private_dir(directory) if directory is not None else Path(tempfile.mkdtemp(prefix="orch-launch-"))
        self._mu = threading.Lock()
        self._files: dict[str, set[Path]] = {}
        for old in self.dir.glob("launch-*.html"):  # left behind by a client that did not exit cleanly
            old.unlink(missing_ok=True)

    def open(self, key: str, url: str) -> bool:
        """Write the redirect file for ``url`` (``key`` names the origin whose redemption removes it) and give the
        browser its path. Returns what the opener returned."""
        path = self.dir / f"launch-{secrets.token_hex(16)}.html"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(
                '<!doctype html><meta name="referrer" content="no-referrer">'
                f"<script>location.replace({json.dumps(url)});</script>"
                f'<noscript><meta http-equiv="refresh" content="0;url={html.escape(url, quote=True)}"></noscript>'
            )
        with self._mu:
            self._files.setdefault(key, set()).add(path)
        timer = threading.Timer(self.ttl, lambda: self._drop(key, path))
        timer.daemon = True
        timer.start()
        try:
            return bool(self.opener(path.resolve().as_uri()))
        except BaseException:
            self._drop(key, path)
            raise

    def redeemed(self, key: str) -> None:
        """The code of this origin was used: its files have done their job."""
        with self._mu:
            paths = self._files.pop(key, set())
        for p in paths:
            p.unlink(missing_ok=True)

    def _drop(self, key: str, path: Path) -> None:
        path.unlink(missing_ok=True)
        with self._mu:
            self._files.get(key, set()).discard(path)

    def close(self) -> None:
        with self._mu:
            keys = list(self._files)
        for k in keys:
            self.redeemed(k)
        if self._own_tmp:
            try:
                self.dir.rmdir()
            except OSError:
                pass
