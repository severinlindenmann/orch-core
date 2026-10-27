"""Direct receive for webhook doorbells (#717): a small HTTP listener inside ``orch host run``.

``POST /h/<hook id>`` always answers ``204`` with the same bytes, whether the hook exists, rang, or is unknown.
The request body is never stored, forwarded or put anywhere: only when a hook has an HMAC secret (GitHub's
``X-Hub-Signature-256``) is the body read, at most 64 KiB, to verify the signature, and it is dropped right after.
The secret lives in ``<state>/hosts/hook-secrets.json`` (0600) on this host only; ``orch host hook-secret`` writes it.
A ring is just ``(H(hook id), now)`` handed to the workspace's follow-up runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import re
import time
from pathlib import Path

from orch.model import followup_events as fe

from . import custody

LOG = logging.getLogger(__name__)
ANSWER = b"HTTP/1.1 204 No Content\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
PATH = re.compile(rb"/h/(hk_[0-7][0-9A-HJKMNP-TV-Z]{25})")
RATE_PER_MIN = 60  # per source address


def secrets_path(state_dir: Path) -> Path:
    return Path(state_dir) / "hosts" / "hook-secrets.json"


def set_secret(state_dir: Path, host_uid: int, hook_id: str, secret: str) -> None:
    """Keep the HMAC secret of one hook (keyed by the hook hash) on this host."""
    path = secrets_path(state_dir)
    custody.ensure_private_dir(path.parent, host_uid)
    doc = custody.read_json(path, host_uid) or {}
    doc[fe.hook_hash(hook_id)] = secret
    custody.write_private(path, doc)


class HookReceiver:
    def __init__(self, host, listen: str, *, clock=time.monotonic):
        addr, _, port = listen.rpartition(":")
        self.addr, self.port = addr or "127.0.0.1", int(port)
        self.host, self.clock = host, clock
        self.server: asyncio.base_events.Server | None = None
        self._seen: dict[str, list[float]] = {}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.addr, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        LOG.info("webhook receiver listening on %s:%s", self.addr, self.port)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None

    def _limited(self, who: str) -> bool:
        now = self.clock()
        hits = [t for t in self._seen.get(who, []) if now - t < 60]
        hits.append(now)
        self._seen[who] = hits
        if len(self._seen) > 1024:
            self._seen = {k: v for k, v in self._seen.items() if v and now - v[-1] < 60}
        return len(hits) > RATE_PER_MIN

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = (writer.get_extra_info("peername") or ("?",))[0]
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as exc:
                LOG.info("webhook request without a complete head: %s", type(exc).__name__)
                head = b""
            if head and len(head) <= MAX_HEAD and not self._limited(peer):
                await self._ring(head, reader)
        except Exception:  # noqa: BLE001 - a bad request must not look different from a good one
            LOG.warning("webhook request failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.write(ANSWER)
                await asyncio.wait_for(writer.drain(), 5)
                writer.close()

    async def _ring(self, head: bytes, reader: asyncio.StreamReader) -> None:
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        m = PATH.fullmatch(parts[1].split(b"?")[0]) if len(parts) == 3 and parts[0] == b"POST" else None
        if m is None:
            LOG.info("webhook request ignored: not POST /h/<id>")
            return
        hook_hash = fe.hook_hash(m.group(1).decode())
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        secret = (custody.read_json(secrets_path(self.host.state_dir), self.host.host_uid) or {}).get(hook_hash)
        if secret is not None and not await self._signed(headers, reader, secret):
            LOG.info("webhook ring refused: bad or missing signature")
            return
        # the body (if any) is never read past this point: the doorbell carries nothing
        for service in list(self.host.services.values()):
            if getattr(service, "followups", None) is None and hasattr(self.host, "_start_followups"):
                await self.host._start_followups(service)  # the journal check may not have started it yet
            runner = getattr(service, "followups", None)
            if runner and await runner.ring(hook_hash):
                return

    @staticmethod
    async def _signed(headers: dict, reader: asyncio.StreamReader, secret: str) -> bool:
        sig = headers.get(b"x-hub-signature-256", b"").decode("latin-1")
        try:
            size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return False
        if not sig.startswith("sha256=") or not 0 <= size <= MAX_BODY:
            return False
        body = await asyncio.wait_for(reader.readexactly(size), 10) if size else b""
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.removeprefix("sha256="), want)
