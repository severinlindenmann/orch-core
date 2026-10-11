"""``orch sign CODE``: the terminal signer of a hand-off request (H5b, §0.8.1 "Hand-off (F5)", §4).

A preparer (the dashboard, or any process of the person's UID in tier A) calls ``human.prepare {handoff:true}`` and
shows the person a code. ``orch sign CODE``, at the person's own terminal:

1. says **"only sign a code you created yourself"**, every time it runs (in tier A any process of the person's UID can
   create a request);
2. resolves the code with ``human.pending {code}`` on the workspace socket (the code names the request, it authorises
   nothing);
3. fetches and binds the request to this machine's device with ``human.get {request_id, signer}``; it never calls
   ``human.prepare`` a second time;
4. runs the signer's checks (:func:`orch.host.signer.check`: shape, every hash, every review, the fingerprint), renders
   the review from ``material`` with the header first (the workspace from this person's **own** registry, the op, the
   origin, the fingerprint), asks for the confirmation word on ``/dev/tty``, signs (the passphrase prompt) and calls
   ``human.submit``; a grant's secret is shown on the terminal only after the commit.

:func:`sign_code` is the library function (one connection, injected backend and prompts); :func:`main` finds the
workspace in the person's registry, opens the device key and connects. It talks to the workspace socket directly with
:class:`orch.host.client.Connection` (it does not need the host-mode CLI of H4b).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from . import signer
from .errors import HostError

__all__ = ["NOTICE", "CODE", "main", "normalise_code", "sign_code"]

NOTICE = (
    "orch sign: only sign a code you created yourself. "
    "Any process running as your user can create a request and show you a code."
)
CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")


def normalise_code(text: str) -> str:
    """The code as the host stores it: upper case, with the dash (``7f3kq2ma`` → ``7F3K-Q2MA``). ``invalid.input``
    for anything that is not 8 Crockford base32 characters."""
    raw = text.strip().upper().replace("-", "")
    code = f"{raw[:4]}-{raw[4:]}"
    if len(raw) != 8 or not CODE.fullmatch(code):
        raise HostError("invalid.input", "a hand-off code is 8 characters, like 7F3K-Q2MA")
    return code


def sign_code(
    call: Callable[..., Any],
    code: str,
    *,
    backend: Any,
    key_id: str,
    workspace_id: str,
    person: str,
    device: str,
    workspace: Callable[[str], tuple[str, str] | None],
    confirm: Callable[[str, str], bool],
    show_secret: Callable[[str], None],
    idem: str,
    resolved: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve ``code``, bind, check, review, confirm, sign and submit (see the module doc). ``call(op, args,
    idem=None)`` sends one request on the pinned workspace connection and returns its ``result``. ``resolved`` is the
    ``human.pending {code}`` answer when the caller already has it (one lookup, not two). Returns ``human.submit``'s
    result. Raises :class:`HostError` (the host's refusals, or ``not_found`` for a code naming nothing) or
    :class:`orch.host.signer.SignerRefused` (nothing is signed)."""
    found = resolved if resolved is not None else call("human.pending", {"code": code})["data"]["requests"]
    if len(found) != 1:
        raise HostError("not_found", f"no pending request of yours has the code {code} in this workspace")
    request_id = found[0]["id"]
    prepared = call("human.get", {"request_id": request_id, "signer": {"device": device, "auth": backend.auth}})["data"]
    try:
        req = prepared.get("request") or {}
        if req.get("id") != request_id or req.get("code") != code:
            signer._refuse("the host answered with another request than the code names")
        signer.check(prepared, workspace_id=workspace_id, person=person, device=device, auth=backend.auth)
        text = f"{NOTICE}\n\n{signer.render(prepared, workspace)}"
        if not confirm(text, signer.confirm_word(prepared)):
            signer._refuse("not confirmed")
        signed = signer.sign(prepared, backend=backend, key_id=key_id, workspace_id=workspace_id)
    except BaseException:
        try:
            call("human.cancel", {"request_id": request_id})  # the preparer learns it from human.pending
        except Exception:  # noqa: BLE001 - it expires anyway
            pass
        raise
    result = call("human.submit", {"request_id": request_id, "events": signed.events}, idem=idem)
    if signed.secret is not None:
        show_secret(f"\nORCH_GRANT={signed.secret}\n(shown once; orch keeps only its hash)\n")
    return result


# --- the command -------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch sign", description="sign a hand-off request at your own terminal")
    p.add_argument("code", help="the code the preparer showed, like 7F3K-Q2MA")
    p.add_argument("--workspace", help="the workspace id or prefix (default: every workspace in your registry)")
    p.add_argument("--state", type=Path)
    p.add_argument("--run", type=Path)
    p.add_argument("--json", action="store_true")
    return p


def _socket(run: Path | None, state: Path, host_uid: int, workspace_id: str) -> Path:
    """``--run``, else the service-managed run directory when a host serves there, else the dev ``<state>/run``."""
    from .listener import default_run_directory

    if run is not None:
        return run / workspace_id / "host.sock"
    try:
        service = default_run_directory(host_uid) / workspace_id / "host.sock"
    except Exception:  # noqa: BLE001 - an unsupported platform has no service run directory
        service = None
    if service is not None and service.exists():
        return service
    return state / "run" / workspace_id / "host.sock"


def _own_workspaces(state: Path, uid: int, pick: str | None) -> list[dict[str, Any]]:
    """The workspaces of this person's own registry where this UID is mapped to a person (never workspace config)."""
    from .custody import read_json
    from .registry import registry_path

    doc = read_json(registry_path(state), uid) or {"workspaces": []}
    out = []
    for item in doc.get("workspaces") or []:
        peer = (item.get("peers") or {}).get(str(uid)) or {}
        if peer.get("class") != "person" or not peer.get("person"):
            continue
        if pick is not None and pick not in (item.get("workspace_id"), item.get("prefix")):
            continue
        out.append(item)
    return out


def _backend(state: Path, workspace_id: str) -> Any:
    from orch.ops import human

    directory = human.keys_dir(state, workspace_id)
    if not (directory / human.KEY_FILE).is_file():
        raise HostError("custody.no_key", "this machine holds no device key of yours for this workspace")
    return human.open_backend(directory)


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
    err.write(NOTICE + "\n")
    conn: Connection | None = None
    try:
        if env.get("ORCH_GRANT"):
            raise HostError("human_only", "orch sign is a person's command; an agent never signs")
        code = normalise_code(args.code)
        state = args.state.resolve() if args.state is not None else state_dir(env)
        uid = os.getuid()
        entries = _own_workspaces(state, uid, args.workspace)
        if not entries:
            raise HostError("not_found", "no workspace in your registry maps you to a person")
        matches = []
        for entry in entries:  # the code names a request in one workspace; ask each of yours (one lookup each)
            wid = entry["workspace_id"]
            pin = Pin(int(entry["host_uid"]), wid, _socket(args.run, state, int(entry["host_uid"]), wid))
            try:
                c = Connection(pin, client="orch sign")
            except (OSError, ConnectionError, ValueError):
                continue  # not served now: the code cannot be there
            try:
                found = c.call("human.pending", {"code": code})["data"]["requests"]
            except BaseException:
                c.close()
                for _, other, _ in matches:
                    other.close()
                raise
            if found:
                matches.append((entry, c, found))
            else:
                c.close()
        if len(matches) != 1:
            for _, c, _ in matches:
                c.close()
            if not matches:
                raise HostError("not_found", f"no pending request of yours has the code {code}")
            raise HostError("invalid.input", f"the code {code} names requests in several workspaces; use --workspace")
        entry, conn, found = matches[0]
        wid = entry["workspace_id"]
        person = entry["peers"][str(uid)]["person"]
        backend = _backend(state, wid)
        device = device_ref(backend.public_key(human.KEY_ID))
        name = Path(str(entry.get("root") or wid)).name

        def workspace(w: str) -> tuple[str, str] | None:
            return (name, str(entry.get("prefix"))) if w == wid else None

        result = sign_code(
            conn.call,
            code,
            backend=backend,
            key_id=human.KEY_ID,
            workspace_id=wid,
            person=person,
            device=device,
            workspace=workspace,
            confirm=human.review_prompt,
            show_secret=human.show_secret,
            idem=new_idem(),
            resolved=found,
        )
    except signer.SignerRefused as exc:
        return _fail(out, args.json, HostError("invalid.input", f"{exc}; nothing was signed"))
    except HostError as exc:
        return _fail(out, args.json, exc)
    except (OSError, ConnectionError) as exc:
        error = HostError("host.unavailable", str(exc)[:400])
        return _fail(out, args.json, error, exit_code=9)
    except Exception as exc:  # noqa: BLE001 - custody refusals (no terminal, wrong passphrase) and the like
        code_name = getattr(exc, "code", None) or type(exc).__name__
        return _fail(out, args.json, HostError(str(code_name), str(exc)[:400]), exit_code=5)
    finally:
        if conn is not None:
            conn.close()
    if args.json:
        out.write(json.dumps({"ok": True, "result": result}, sort_keys=True) + "\n")
    else:
        on = f" on {result['key']}" if result.get("key") else ""
        out.write(f"signed {found[0]['op']}{on}: seq {result.get('seq')}\n")
    return 0


def _fail(out: TextIO, as_json: bool, exc: HostError, exit_code: int | None = None) -> int:
    if as_json:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
    else:
        out.write(f"orch sign: {exc.code}: {exc.message}\n")
    return exit_code if exit_code is not None else exc.exit
