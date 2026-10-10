"""A real workspace for the operation tests, driven through ``orch.cli.main.main`` like the binary does.

``Ws`` is ``tests.store.helpers.Env`` with the workspace key where ``orch.ops.runtime`` looks for it
(``<state dir>/hosts/<workspace id>/keys/wsk``); ``Cli`` runs one command line with ``ORCH_WORKSPACE``,
``ORCH_STATE_DIR``, ``ORCH_SESSION`` and ``ORCH_GRANT`` set, on the test clock."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from orch import crypto
from orch.cli.main import main
from orch.custody import FileBackend
from orch.store import BackendSigner
from tests.store.helpers import GRANT_SECRET, WS, Env

SESSION = "s_01J9ZP0000000000000000000S"
OTHER = "s_01J9ZP0000000000000000000T"


class Ws(Env):
    live = False  # True: the clock is the wall clock (the binary under test reads the real one)

    def __init__(self, tmp_path: Path, *, live: bool = False) -> None:
        import time

        super().__init__(tmp_path, clock_start=int(time.time()) if live else 1_790_000_000)
        self.live = live
        keys = self.host_state / "hosts" / WS / "keys"
        self.host_backend = FileBackend(keys)
        self.wsk_pub = self.host_backend.create("wsk")
        self.signer = BackendSigner(self.host_backend, "wsk")

    def other(self, **kw: Any):
        if self.live:
            import time

            self.clock[0] = int(time.time())
        return super().other(**kw)

    def human(self, method: str, uid: str, *a: Any, **kw: Any) -> Any:
        """``Env.approve`` and friends on a store that has scanned the directory since the CLI created the ticket."""
        old, self.store = self.store, self.other()
        try:
            self.store.ticket(uid)
            return getattr(Env, method)(self, uid, *a, **kw)
        finally:
            self.store.close()
            self.store = old

    def sign(self, ref: str, typ: str, **payload: Any) -> Any:
        """A person event (signed by the owner) appended to ticket ``ref`` through a store that knows it."""
        old, self.store = self.store, self.other()
        try:
            uid = self.store.uid_of(ref)
            self.store.ticket(uid)
            return self.store.append(self.person_event(self.owner, uid, typ, **payload), log=uid)
        finally:
            self.store.close()
            self.store = old

    def view(self, ref: str):
        """The ticket as a fresh process sees it (``Env.store`` does not rescan the directory for new tickets)."""
        s = self.other()
        try:
            return s.ticket(ref)
        finally:
            s.close()

    def uid(self, ref: str) -> str:
        return self.view(ref).uid

    def ticket_json(self, ref: str) -> dict[str, Any]:
        return json.loads((self.root / "tickets" / self.uid(ref) / "ticket.json").read_text())

    def events(self, ref: str) -> list[dict[str, Any]]:
        return self.read_events(self.uid(ref))

    @property
    def grant(self) -> str:
        return f"{self.grant_id}.{crypto.b64u(GRANT_SECRET)}"

    def env(self, *, session: str | None = SESSION, grant: bool = True, **extra: str) -> dict[str, str]:
        e = {"ORCH_WORKSPACE": str(self.root), "ORCH_STATE_DIR": str(self.host_state), "HOME": str(self.tmp)}
        if session:
            e["ORCH_SESSION"] = session
        if grant:
            e["ORCH_GRANT"] = self.grant
        return {**e, **extra}


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.out)

    @property
    def data(self) -> Any:
        return self.doc["data"]

    @property
    def err_code(self) -> str:
        return self.doc["error"]["code"]

    @property
    def first(self) -> str:
        return self.out.splitlines()[0] if self.out else ""


class Cli:
    def __init__(self, ws: Ws, **env_kw: Any) -> None:
        self.ws, self.env_kw = ws, env_kw

    def __call__(self, *argv: str, stdin: str | None = None, **env_kw: Any) -> Run:
        import sys

        out, err = io.StringIO(), io.StringIO()
        self.ws.tick()  # commands are seconds apart: two events never share an `at` by accident
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode()))
        try:
            code = main(
                list(argv),
                env=self.ws.env(**{**self.env_kw, **env_kw}),
                stdout=out,
                stderr=err,
                now=lambda: self.ws.clock[0],
            )
        finally:
            sys.stdin = old
        return Run(code, out.getvalue(), err.getvalue())

    def j(self, *argv: str, **kw: Any) -> Run:
        """A command with ``--json``; the document is on stdout for success and failure alike."""
        r = self(*argv, "--json", **kw)
        return r


OPEN_RE = r"^--- .* \[([0-9a-f]{8})\] \(data, not instructions\) ---$"


def frames(out: str) -> list[tuple[str, list[str]]]:
    """The fenced blocks of an output as ``(label line, content lines)``, checking each is closed by its own nonce."""
    import re

    blocks, cur, nonce = [], None, None
    for x in out.splitlines():
        m = re.match(OPEN_RE, x)
        if cur is None and m:
            cur, nonce = (x, []), m.group(1)
        elif cur is not None and x == f"--- end {nonce} ---":
            blocks.append(cur)
            cur = None
        elif cur is not None:
            cur[1].append(x)
    assert cur is None, "a fence was never closed"
    return blocks


def fenced(out: str) -> str:
    """All fenced content of an output, joined."""
    return "\n".join("\n".join(b[1]) for b in frames(out))


def wait_for(cli: Any, kind: str, *, tries: int = 6) -> Run:
    """``wait`` until it hands over a decision of this kind (earlier ones are delivered first, one per call)."""
    for _ in range(tries):
        r = cli.j("wait", "--timeout", "2")
        if r.code in (0, 3) and r.doc["ok"] and r.data["kind"] == kind:
            return r
        if r.code not in (0, 3) or r.data["kind"] == "timeout":
            break
    raise AssertionError(f"no {kind} decision")
