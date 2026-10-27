"""A fake for :class:`orch.setup.steps.Ports`: records every call, serves ``human.prepare`` from the plan."""

from __future__ import annotations

import contextlib
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orch.setup import steps
from orch.setup.steps import Genesis, Identity

WID = "0123456789abcdef0123456789abcdef"
PERSON = "p_" + "1" * 32
DEVICE = "d_" + "2" * 32


class FakeTerminal:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.shown: list[str] = []
        self.asked: list[str] = []
        self.answers = answers or []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def ask_secret(self, prompt: str) -> str:
        self.asked.append(prompt)
        self.secret_asked = getattr(self, "secret_asked", 0) + 1
        return self.secrets.pop(0) if getattr(self, "secrets", None) else ""

    def wait(self, prompt: str) -> None:
        self.asked.append(prompt)


class FakeLink:
    def __init__(self, ports: FakePorts) -> None:
        self.p = ports
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def call(self, op: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.p.log.append(("call", op))
        if op == "human.prepare":
            key = (args["op"], json.dumps(args["args"], sort_keys=True))
            ev = copy.deepcopy(self.p.events[key])
            if self.p.tamper:
                self.p.tamper(ev)
            if ev["type"] == "grant.issued":
                now = self.p.now
                iso = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
                ev.update(
                    grant="gr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    issued_at=iso(now),
                    expires_at=iso(now + 3600 * ev["hours"]),
                    secret_hash=None,
                )
            self.p.n += 1
            rid = f"rq_01ARZ3NDEKTSV4RRFFQ69G5F{self.p.n:02d}"[:29]
            return {"data": {"op": args["op"], "events": [ev], "request": {"id": rid}}}
        if op == "human.submit":
            if self.p.fail_submit == self.p.submitted:
                raise RuntimeError("submit failed")
            self.p.submitted += 1
            self.p.submits.append(args["events"][0]["type"])
            return {"data": {}}
        if op == "human.cancel":
            self.p.cancelled.append(args["request_id"])
            return {"data": {}}
        if op == "grant.deposit":
            return {"data": {"deposit": self.p.deposit}}
        if op == "grant.deposit_status":
            return {"data": {"deposit": self.p.deposit}}
        if op == "whoami":
            return {"data": {"runner": {"connected": self.p.hello}}}
        raise AssertionError(op)


class FakePorts:
    def __init__(self, terminal: FakeTerminal | None = None) -> None:
        self.terminal = terminal or FakeTerminal()
        self.log: list[tuple] = []
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.tamper = None
        self.now = 1_800_000_000.0
        self.n = 0
        self.hello = True
        self.deposit = "confirmed"
        self.fail_genesis: Exception | None = None
        self.fail_runner = False
        self.fail_stage = 0  # number of stage attempts that fail
        self.fail_finish = 0
        self.fail_submit: int | None = None
        self.submitted = 0
        self.submits: list[str] = []
        self.cancelled: list[str] = []
        self.unlocks = 0
        self.passphrase_seen: str | None = None
        self.code_seen: str | None = None
        self.staged_undone = 0
        self.interrupt_after_stage = False

    def expect(self, plan) -> None:
        for it in plan.items:
            self.events[(it.op, json.dumps(it.args, sort_keys=True))] = copy.deepcopy(it.event)

    # -- Ports
    def genesis(self, *, root: Path, profile: dict, passphrase: str, code: str) -> Genesis:
        self.log.append(("genesis", profile["prefix"]))
        self.passphrase_seen, self.code_seen = passphrase, code
        if self.fail_genesis:
            raise self.fail_genesis
        return Genesis(WID, PERSON, DEVICE)

    def identity(self, wid: str) -> Identity:
        return Identity(PERSON, DEVICE, "passphrase")

    def install_addon(self, root: Path, name: str, raw: bytes) -> None:
        self.log.append(("install_addon", name))

    def git_init(self, root: Path) -> None:
        self.log.append(("git_init", str(root)))

    def trust_folder(self, root: Path) -> bool:
        self.log.append(("trust_folder", str(root)))
        return True

    def records_setup(self, wid: str, root: Path) -> None:
        self.log.append(("records_setup", wid, str(root)))
        if getattr(self, "fail_records", False):
            raise RuntimeError("no git")

    def runner_enable(self, wid: str, prefix: str, terminals: bool) -> None:
        self.log.append(("runner_enable", terminals))
        if self.fail_runner:
            raise RuntimeError("no service")

    def runner_refresh(self) -> None:
        self.log.append(("runner_refresh",))

    def runner_hello(self, wid: str, timeout: float) -> bool:
        self.log.append(("runner_hello",))
        return self.hello

    def workspace(self, wid: str) -> FakeLink:
        self.log.append(("workspace",))
        return FakeLink(self)

    @contextlib.contextmanager
    def unlock(self, wid, passphrase, digest, fields):
        self.unlocks += 1
        self.log.append(("unlock",))
        yield steps_backend()
        self.log.append(("lock",))

    def convenience_on(self, wid: str) -> None:
        self.log.append(("convenience_on",))

    def stage(self, wid: str, prefix: str, passphrase: str):
        self.log.append(("stage",))
        if self.fail_stage > 0:
            self.fail_stage -= 1
            raise RuntimeError("keychain locked")
        return {"staged": True}

    def finish_conversion(self, wid: str, staged) -> None:
        self.log.append(("finish_conversion",))
        if self.interrupt_after_stage:
            raise KeyboardInterrupt
        if self.fail_finish > 0:
            self.fail_finish -= 1
            raise RuntimeError("could not read back")

    def undo(self, wid: str, staged) -> None:
        self.log.append(("undo",))
        self.staged_undone += 1


def steps_backend():
    return object()


def names(ports: FakePorts) -> list[str]:
    return [e[0] if e[0] != "call" else e[1] for e in ports.log]


__all__ = ["DEVICE", "PERSON", "WID", "FakePorts", "FakeTerminal", "names", "steps"]
