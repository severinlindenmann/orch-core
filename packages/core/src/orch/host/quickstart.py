"""``orch quick start TITLE``: a quick task as a person, on ONE review and ONE confirmation (batch 11, order step ii).

The same sequence the dashboard uses (:mod:`orch.host.seqquick`): the signer shows the whole task and what it
approves once, you confirm once, and it signs the ticket (a chore labelled ``quick``), its requirements, one
criterion and one task per ``--step``, the plan it derives, both approvals and, only with ``--agents HOURS``, the
agent access, last. The task still ends with your own verdict. This is a person's command: it needs your device
key and your terminal, and an agent never reaches it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from . import sequence, sequence_terminal, signer, signer_registry
from .errors import HostError

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch quick start", description="start a quick task: one review, one confirmation")
    p.add_argument("title", help="one line, at most 200 characters")
    p.add_argument("--details", default="", help="what is wrong and what should be true afterwards")
    p.add_argument("--step", action="append", default=[], metavar="TEXT", help="a step (up to 4); repeat")
    p.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="CMD",
        help="the command that checks the step with the same position",
    )
    p.add_argument("--agents", type=int, metavar="HOURS", help="let agents work on any ticket for this many hours")
    p.add_argument("--workspace", help="the workspace id or prefix")
    p.add_argument("--state", type=Path)
    p.add_argument("--run", type=Path)
    p.add_argument("--json", action="store_true")
    return p


def build_intent(args: argparse.Namespace) -> dict:
    from orch.canon.text import TextError, normalize_text

    if len(args.check) > len(args.step):
        raise HostError("invalid.input", "--check names a command for a --step: give the step first")
    try:
        title = normalize_text(args.title, one_line=True)
        description = normalize_text(args.details).strip("\n") or title
        checks = [
            {
                "text": normalize_text(t, one_line=True),
                "cmd": normalize_text(args.check[i], one_line=True) if i < len(args.check) else None,
            }
            for i, t in enumerate(args.step)
        ] or [{"text": title, "cmd": None}]
        return sequence.validate_intent(
            {
                "v": 1,
                "kind": "quick_task",
                "title": title,
                "description": description,
                "checks": checks,
                "agent": None if args.agents is None else {"hours": args.agents, "scope": "workable"},
            }
        )
    except (TextError, sequence.SequenceInvalid) as exc:
        raise HostError("invalid.input", f"that task cannot be signed: {exc}") from None


def main(
    argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    from orch.host.client import Connection, Pin
    from orch.host.control_client import new_idem
    from orch.identity import device_ref
    from orch.ops import human
    from orch.ops.runtime import state_dir

    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    conn = None
    try:
        if "ORCH_GRANT" in env:
            raise HostError("human_only", "orch quick start is a person's command; an agent never signs")
        intent = build_intent(args)
        state = args.state.resolve() if args.state is not None else state_dir(env)
        uid = os.getuid()
        entries = signer_registry._own_workspaces(state, uid, args.workspace)
        if len(entries) != 1:
            raise HostError(
                "invalid.input", "name the workspace with --workspace (none or several map you to a person)"
            )
        entry = entries[0]
        wid, person = entry["workspace_id"], entry["peers"][str(uid)]["person"]
        backend = signer_registry._backend(state, wid)
        device = device_ref(backend.public_key(human.KEY_ID))
        name = Path(str(entry.get("root") or wid)).name
        path = signer_registry._socket(args.run, state, int(entry["host_uid"]), wid)
        conn = Connection(Pin(int(entry["host_uid"]), wid, path), client="orch quick")
        op, op_args = sequence.step_args(intent, 0, None)
        meta = {
            "step": 0,
            "of": len(sequence.steps(intent)),
            "digest": sequence.digest_of(wid, person, intent),
            "intent": intent,
        }
        first = conn.call(
            "human.prepare",
            {"op": op, "args": op_args, "signer": {"device": device, "auth": backend.auth}, "sequence": meta},
        )["data"]
        result = sequence_terminal.sign_sequence(
            conn.call,
            first,
            first["request"]["id"],
            backend=backend,
            key_id=human.KEY_ID,
            workspace_id=wid,
            person=person,
            device=device,
            workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
            confirm=human.review_prompt,
            sessions=sequence.Sessions(state),
            new_idem=new_idem,
            deliver=lambda secret, rid: signer.deposit_lines(signer.hand_over(conn.call, secret, rid[3:])),
        )
    except sequence.Aborted as exc:
        text = f"stopped after {exc.landed} of {exc.total} signatures: {exc.reason}"
        if exc.landed:
            text += "; what was signed stays in the ticket"
        return _fail(out, args.json, HostError("human.request_mismatch", text))
    except signer.SignerRefused as exc:
        return _fail(out, args.json, HostError("invalid.input", f"{exc}; nothing was signed"))
    except human.OrchError as exc:
        return _fail(out, args.json, HostError(exc.code, str(exc)))
    except HostError as exc:
        return _fail(out, args.json, exc)
    except (OSError, ConnectionError) as exc:
        return _fail(out, args.json, HostError("host.unavailable", str(exc)[:400]), exit_code=9)
    finally:
        if conn is not None:
            conn.close()
    if args.json:
        out.write(json.dumps({"ok": True, "result": result}, sort_keys=True) + "\n")
    else:
        out.write(f"started {result['key']}: {result['steps']} signatures, one confirmation\n")
        for line in result.get("lines") or []:
            out.write(line + "\n")
    return 0


def _fail(out: TextIO, as_json: bool, exc: HostError, exit_code: int | None = None) -> int:
    if as_json:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
    else:
        out.write(f"orch quick start: {exc.code}: {exc.message}\n")
    return exc.exit if exit_code is None else exit_code
