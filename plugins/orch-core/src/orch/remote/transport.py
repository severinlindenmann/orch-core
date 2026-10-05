"""The transport child: `<relay tool> bridge-host --workspace <space> [--take-over]`, a long-running process that
holds this device's relay credentials and passes mailbox operations over a pipe. The orch process never holds a
relay credential: it writes JSON-lines commands to the child's stdin and reads events and answers from its stdout.
Bodies are sealed bytes (base64url text) that the child passes on unread. The child's stderr is free text for the
human and goes to the terminal as it is.

Protocol (one JSON object per line): first line out `{"event": "ready", "holder", "lease_s"}`; then each command
`{"id", "op", ...}` is answered `{"id", "ok": true, ...}` or `{"id", "ok": false, "code", "message"}`, possibly out of
order (a poll can wait 25 s while a respond goes through at once).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os

LINE_LIMIT = 32 << 20  # one answer line: a poll may carry several sealed requests of up to 1 MiB each
READY_TIMEOUT = 30.0
CALL_TIMEOUT = 30.0
CODES = frozenset({"host_taken", "lease_lost", "not_owner", "unauthorized", "pending", "rate_limited", "too_large",
                   "bad_request", "no_space", "network", "server", "protocol"})


class TransportError(Exception):
    """A command failed: `code` is one of CODES, or `exited` when the child is gone."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Child:
    """One transport child. `argv` is the full command line (the relay tool and its arguments); `cwd` is the
    workspace root, where the tool finds its device configuration."""

    def __init__(self, argv: list[str], cwd):
        self.argv, self.cwd = list(argv), cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.ready: dict | None = None
        self._next = 0
        self._waiting: dict[str, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._ready = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self._reader is not None \
            and not self._reader.done()

    async def start(self, timeout: float = READY_TIMEOUT) -> dict:
        self._ready = asyncio.get_running_loop().create_future()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None,
                cwd=str(self.cwd), limit=LINE_LIMIT, env=os.environ.copy())
        except OSError as e:
            raise TransportError("exited") from e
        self._reader = asyncio.create_task(self._read())
        try:
            self.ready = await asyncio.wait_for(asyncio.shield(self._ready), timeout)
        except asyncio.TimeoutError:
            raise TransportError("exited") from None
        return self.ready

    async def _read(self) -> None:
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):  # a line over the limit: the pipe is out of step
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("event") == "ready" and not self._ready.done():
                    self._ready.set_result(msg)
                    continue
                fut = self._waiting.pop(msg.get("id"), None) if isinstance(msg.get("id"), str) else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        finally:
            if not self._ready.done():
                self._ready.set_exception(TransportError("exited"))
                self._ready.exception()
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(TransportError("exited"))
            self._waiting.clear()

    async def call(self, op: str, timeout: float = CALL_TIMEOUT, **args) -> dict:
        """The answer of one command; TransportError(code) when it failed or the child is gone."""
        if not self.alive:
            raise TransportError("exited")
        self._next += 1
        cid = f"c{self._next}"
        fut = asyncio.get_running_loop().create_future()
        self._waiting[cid] = fut
        try:
            self.proc.stdin.write((json.dumps({"id": cid, "op": op, **args}, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TransportError("network") from None
        except (OSError, RuntimeError) as e:  # the pipe closed under us
            raise TransportError("exited") from e
        finally:
            self._waiting.pop(cid, None)
        if msg.get("ok") is True:
            return msg
        code = msg.get("code")
        raise TransportError(code if code in CODES else "protocol")

    async def close(self, timeout: float = 5.0) -> None:
        """End the child: close its stdin (it releases the lease and exits), then terminate, then kill; only this
        child's own process."""
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(OSError, RuntimeError):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
