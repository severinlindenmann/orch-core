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
   ``human.submit``; a grant's secret is shown on the terminal only after the commit (:func:`orch.host.signer.finish`,
   the tail the composite shares). Artifact digests are labelled a signed commitment this signer has not seen, unless
   ``--file NAME=PATH`` compares them with local files first.

The search over the registry fails closed (:func:`_find`): a pin failure is ``host.untrusted``; one workspace's failure
is reported, not fatal; nothing is signed unless exactly one workspace holds the code.

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

from . import sequence, signer
from .errors import HostError
from .sequence import Sessions

__all__ = ["NOTICE", "CODE", "main", "normalise_code", "sign_code"]

NOTICE = (
    "orch sign: only sign a code you created yourself. "
    "Any process running as your user can create a request and show you a code."
)
CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")
#: H11c: asked after a runner grant's commit (§12.4.2)
DEPOSIT_PROMPT = "\n[r] hand to the orch runner  [s] show ORCH_GRANT here: "


def normalise_code(text: str) -> str:
    """The code as the host stores it, upper case with its dash: ``7f3k-q2ma`` or ``7F3KQ2MA`` (surrounding spaces
    trimmed) → ``7F3K-Q2MA``. Only that shape: 8 Crockford base32 characters with at most one dash, after the fourth.
    ``invalid.input`` otherwise."""
    raw = text.strip().upper()
    if len(raw) == 8:
        raw = f"{raw[:4]}-{raw[4:]}"
    if not CODE.fullmatch(raw):
        raise HostError("invalid.input", "a hand-off code is 8 characters, like 7F3K-Q2MA")
    return raw


def _deliver(
    call: Callable[..., Any],
    secret: str,
    request_id: str,
    choose: Callable[[str], str] | None,
    show_secret: Callable[[str], None],
) -> list[str]:
    """A sequence's runner grant, after its commit: to the orch runner when the person chooses it on their own
    terminal, else shown there once (the same choice as a single grant, H11c)."""
    if choose is not None:
        try:
            wanted = choose(DEPOSIT_PROMPT)
        except Exception:  # noqa: BLE001 - no terminal to ask on: shown
            wanted = "s"
        if wanted == "r":
            return signer.deposit_lines(signer.hand_over(call, secret, request_id[3:]))
    show_secret(signer.secret_text(secret))
    return []


def _local_files(prepared: Mapping[str, Any], files: Mapping[str, bytes]) -> None:
    """``--file NAME=PATH``: every artifact event of the request has a local file of its name whose digest and size are
    the signed ones, and every file names an artifact event. Anything else is refused before the review."""
    from orch import canon

    events = {e.get("name"): e for e in prepared["events"] if e.get("type") in signer.ARTIFACT_TYPES}
    for name in files:
        if name not in events:
            signer._refuse(f"--file {name}: the request has no artifact of that name")
    for name, ev in events.items():
        if name not in files:
            signer._refuse(f"{signer._clean(name)}: no --file for this artifact; the bytes cannot be compared")
        data = files[name]
        if (canon.artifact_digest(data), len(data)) != (ev.get("sha256"), ev.get("bytes")):
            signer._refuse(f"{signer._clean(name)}: the artifact is not your local file")


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
    files: Mapping[str, bytes] | None = None,
    choose: Callable[[str], str] | None = None,
    sessions: Any = None,
    now: Callable[[], float] | None = None,
    notice: bool = True,
    no_passphrase: bool = False,
) -> dict[str, Any]:
    """Resolve ``code``, bind, check, review, confirm, sign and submit (see the module doc). ``call(op, args,
    idem=None)`` sends one request on the pinned workspace connection and returns its ``result``. ``resolved`` is the
    ``human.pending {code}`` answer when the caller already has it (one lookup, not two). ``files`` (``--file``):
    local bytes by artifact name; with them every artifact digest is compared and labelled checked, without them it is
    labelled a signed commitment this signer has not seen. Returns ``human.submit``'s result. Raises
    :class:`HostError` (the host's refusals, or ``not_found`` for a code naming nothing) or
    :class:`orch.host.signer.SignerRefused` (nothing is signed; the request is cancelled)."""
    import base64

    found = resolved if resolved is not None else call("human.pending", {"code": code})["data"]["requests"]
    if len(found) != 1:
        raise HostError("not_found", f"no pending request of yours has the code {code} in this workspace")
    request_id = found[0]["id"]
    prepared = call("human.get", {"request_id": request_id, "signer": {"device": device, "auth": backend.auth}})["data"]

    if (prepared.get("material") or {}).get("sequence") is not None:
        # batch 11, finding 9: the first request of a sequence is signed with its steps, on one review and one
        # confirmation (host/sequence.py); a client without the session store cannot do that
        from . import sequence_terminal
        from .control_client import new_idem

        if sessions is None:
            signer._refuse("this request is a sequence of signatures and this client cannot sign sequences")
        if files:
            signer._refuse("a sequence carries no files")
        req = prepared.get("request") or {}
        if req.get("id") != request_id or req.get("code") != code:
            signer._refuse("the host answered with another request than the code names")
        return sequence_terminal.sign_sequence(
            call,
            prepared,
            request_id,
            backend=backend,
            key_id=key_id,
            workspace_id=workspace_id,
            person=person,
            device=device,
            workspace=workspace,
            confirm=confirm,
            sessions=sessions,
            new_idem=new_idem,
            prefix=_prefix(notice, no_passphrase),
            deliver=lambda secret, rid: _deliver(call, secret, rid, choose, show_secret),
            **({} if now is None else {"now": now}),
        )

    def on_secret(secret: str) -> list[str]:
        """H11c: a runner grant (label ``runner``) may go to the orch runner instead of the screen; the person
        chooses on their own terminal. Any other grant is shown, as before."""
        runner = any(e.get("type") == "grant.issued" and e.get("label") == "runner" for e in prepared["events"])
        if runner and choose is not None:
            try:
                wanted = choose(DEPOSIT_PROMPT)
            except Exception:  # noqa: BLE001 - no terminal to ask on: shown, as without the choice
                wanted = "s"
            if wanted == "r":
                return signer.deposit_lines(signer.hand_over(call, secret, request_id[3:]))
        show_secret(signer.secret_text(secret))
        return []

    def before() -> None:
        req = prepared.get("request") or {}
        if req.get("id") != request_id or req.get("code") != code:
            signer._refuse("the host answered with another request than the code names")
        if files:
            _local_files(prepared, files)

    return signer.finish(
        call,
        prepared,
        request_id,
        backend=backend,
        key_id=key_id,
        workspace_id=workspace_id,
        person=person,
        device=device,
        workspace=workspace,
        confirm=confirm,
        show_secret=show_secret,
        idem=idem,
        files={n: {"b64": base64.b64encode(b).decode()} for n, b in files.items()} if files else None,
        artifacts="checked" if files else "unseen",
        before=before,
        prefix=_prefix(notice, no_passphrase),
        on_secret=on_secret,
    )


# --- the command -------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch sign", description="sign a hand-off request at your own terminal")
    p.add_argument("code", help="the code the preparer showed, like 7F3K-Q2MA")
    p.add_argument("--workspace", help="the workspace id or prefix (default: every workspace in your registry)")
    p.add_argument("--state", type=Path)
    p.add_argument("--run", type=Path)
    p.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="compare the request's artifact NAME with your local file PATH before you sign",
    )
    p.add_argument("--details", action="store_true", help="show the full review (hashes, ids) before the question")
    p.add_argument("--json", action="store_true")
    return p


def _read_files(items: list[str]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise HostError("invalid.input", "--file takes NAME=PATH")
        if name in files:
            raise HostError("invalid.input", f"--file {name} is given twice")
        try:
            files[name] = Path(path).expanduser().read_bytes()
        except OSError as exc:
            raise HostError("invalid.input", f"--file {name}: cannot read {path} ({exc.strerror})") from None
    return files


def _find(entries: list[dict[str, Any]], code: str, run: Path | None, state: Path, err: TextIO) -> tuple[Any, Any, Any]:
    """Ask each workspace of the registry for ``code`` and return ``(entry, open connection, requests)`` of the one that
    holds it. **Fails closed**: nothing is signed unless exactly one workspace answered with the request.

    * A pin failure (the socket's process is not the registry's host UID, or the welcome names another workspace) is a
      hard refusal, ``host.untrusted``, naming the socket: never "not served".
    * A workspace that is not served (no socket, connection refused) is skipped.
    * Any other failure of one workspace (a refusal, ``rate.limited``, ``workspace.unavailable``, a broken connection)
      is collected and the search goes on; when no workspace holds the code, ``not_found`` names each of them.
    * Several workspaces holding the code: ``invalid.input`` (use ``--workspace``).

    Every connection but the returned one is closed, whatever happens."""
    from orch.host import frames
    from orch.host._unix import UnsafePath
    from orch.host.client import Connection, Pin

    matches: list[tuple[dict[str, Any], Any, list[dict[str, Any]]]] = []
    errors: list[str] = []
    unreachable = 0
    try:
        for entry in entries:
            wid = entry["workspace_id"]
            label = f"{entry.get('prefix')} ({wid})"
            path = _socket(run, state, int(entry["host_uid"]), wid)
            try:
                c = Connection(Pin(int(entry["host_uid"]), wid, path), client="orch sign")
            except UnsafePath as exc:
                raise HostError("host.untrusted", f"{path}: {exc}; refusing to talk to it") from None
            except (FileNotFoundError, ConnectionRefusedError):
                continue  # not served now: the code cannot be there
            except (OSError, ConnectionError, frames.FrameError) as exc:
                errors.append(f"{label}: cannot connect ({exc})")
                continue
            try:
                found = c.call("human.pending", {"code": code})["data"]["requests"]
            except HostError as exc:
                c.close()
                if exc.code == "workspace.unavailable":
                    unreachable += 1  # another workspace that cannot answer: plain words, no ids
                else:
                    errors.append(f"{label}: {exc.code}")
                continue
            except (OSError, ConnectionError, frames.FrameError) as exc:
                c.close()
                errors.append(f"{label}: {exc}")
                continue
            except BaseException:
                c.close()
                raise
            if found:
                matches.append((entry, c, found))
            else:
                c.close()
    except BaseException:
        for _, other, _ in matches:
            other.close()
        raise
    if unreachable:
        errors.append(
            "1 other workspace on this computer is not reachable"
            if unreachable == 1
            else f"{unreachable} other workspaces on this computer are not reachable"
        )
    if len(matches) == 1:
        if errors:
            err.write("orch sign: not asked: " + "; ".join(errors) + "\n")
        return matches[0]
    for _, c, _ in matches:
        c.close()
    if matches:
        raise HostError("invalid.input", f"the code {code} names requests in several workspaces; use --workspace")
    also = f" (not asked: {'; '.join(errors)})" if errors else ""
    raise HostError("not_found", f"no pending request of yours has the code {code}{also}")


def _socket(run: Path | None, state: Path, host_uid: int, workspace_id: str) -> Path:
    """``--run``, else the tier-A ``<state>/run`` (H7: a path that merely exists under the tier-B run directory never
    wins)."""
    from .enable import run_dir_for

    return run_dir_for(state, run) / workspace_id / "host.sock"


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


NO_PASSPHRASE = "This key has no passphrase (One click is on): anything running as you could sign with it."


def _prefix(notice: bool, no_passphrase: bool) -> str:
    """The warnings the review carries when stderr is not the terminal (N13): the notice, and the key's custody."""
    lines = ([NOTICE] if notice else []) + ([NO_PASSPHRASE] if no_passphrase else [])
    return "".join(f"{line}\n\n" for line in lines)


def _sign_code_explained(entry: dict[str, Any], run: Callable[..., Any], *a: Any, backend: Any, **kw: Any) -> Any:
    """:func:`sign_code`, with the one refusal the person cannot act on made plain: a key in one-click custody binds
    ``auth: convenience`` (the key has no passphrase), and while click-to-sign is off the host refuses that binding
    (reason ``disabled``). Nothing is signed another way: the request keeps its method, the host's check stands."""
    try:
        return run(*a, backend=backend, **kw)
    except HostError as exc:
        if (
            exc.code == "sign.convenience_unavailable"
            and (exc.detail or {}).get("reason", exc.message.split(":", 1)[0]) == "disabled"
            and getattr(backend, "auth", None) == "convenience"
        ):
            ws = entry.get("prefix") or entry["workspace_id"]
            raise HostError(
                exc.code,
                "disabled: this key is in one-click custody and one-click signing is off, so it cannot sign in the "
                "Terminal. Move the key back to a passphrase, or turn one-click on again.",
                hint=f"orch keys custody passphrase --workspace {ws} --in-place  or  "
                f"orch host convenience on --workspace {ws}",
                detail=exc.detail,
            ) from None
        raise


def _backend(state: Path, workspace_id: str) -> Any:
    """The device key's custody from the person's selection (H5c, orch-v2-host §0.8.6): the convenience backend when
    selected, else the passphrase key at ``<state>/hosts/<ws>/person/`` (H5b's location). The binding is the signer
    tuple, so a convenience selection binds ``auth: "convenience"`` and the host's readers condition applies."""
    from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
    from orch.custody.select import device_backend

    try:
        backend, _ = device_backend(state, workspace_id)
    except KeyNotFound:
        raise HostError("custody.no_key", "this machine holds no device key of yours for this workspace") from None
    except BackendUnavailable as exc:
        raise HostError("custody.unavailable", str(exc)[:400]) from None
    except CustodyError as exc:
        raise HostError("custody.unavailable", str(exc)[:400]) from None
    return backend


def main(
    argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    from orch.host.client import Connection
    from orch.host.control_client import new_idem
    from orch.identity import device_ref
    from orch.ops import human
    from orch.ops.runtime import state_dir

    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    err.write(NOTICE + "\n")
    # N13: on one terminal the warning is read once (here, before anything else); the review carries it only when
    # stderr is somewhere else (a pipe, a file), so the person at the terminal still sees it before signing
    on_tty = bool(getattr(err, "isatty", lambda: False)())
    conn: Connection | None = None
    try:
        if "ORCH_GRANT" in env:
            raise HostError("human_only", "orch sign is a person's command; an agent never signs")
        code = normalise_code(args.code)
        files = _read_files(args.file)
        if args.details:  # B6: the full review first; the plain view and `d` otherwise (signer.plain reads it)
            os.environ["ORCH_SIGN_DETAILS"] = "1"
        state = args.state.resolve() if args.state is not None else state_dir(env)
        uid = os.getuid()
        entries = _own_workspaces(state, uid, args.workspace)
        if not entries:
            raise HostError("not_found", "no workspace in your registry maps you to a person")
        entry, conn, found = _find(entries, code, args.run, state, err)
        wid = entry["workspace_id"]
        person = entry["peers"][str(uid)]["person"]
        backend = _backend(state, wid)
        one_click = getattr(backend, "auth", None) == "convenience"
        if one_click:  # #1170: the person reads what kind of key signs (in the review too when stderr is elsewhere)
            err.write(NO_PASSPHRASE + "\n")
        device = device_ref(backend.public_key(human.KEY_ID))
        name = Path(str(entry.get("root") or wid)).name

        def workspace(w: str) -> tuple[str, str] | None:
            return (name, str(entry.get("prefix"))) if w == wid else None

        result = _sign_code_explained(
            entry,
            sign_code,
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
            files=files or None,
            choose=human.secret_destination,
            sessions=Sessions(state),
            notice=not on_tty,
            no_passphrase=one_click and not on_tty,
        )
    except sequence.Aborted as exc:
        text = f"stopped after {exc.landed} of {exc.total} signatures: {exc.reason}"
        if exc.landed:
            text += "; what was signed stays in the ticket as a draft"
        return _fail(out, args.json, HostError("human.request_mismatch", text))
    except signer.Cancelled:  # Ctrl-C before the signature existed: the request is cancelled
        # a person's own cancel is not an error to decode: no code prefix on the human line (the exit code stays)
        return _fail(out, args.json, HostError("invalid.input", "Cancelled, nothing was signed."), plain=True)
    except KeyboardInterrupt:  # Ctrl-C after the signature: it may have landed, so no claim either way
        text = "Interrupted; what was signed may have landed: check the ticket with orch show"
        return _fail(out, args.json, HostError("invalid.input", text))
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
        more = f" ({result['steps']} signatures)" if result.get("steps") else ""
        out.write(f"signed {found[0]['op']}{on}{more}: seq {result.get('seq')}\n")
    return 0


def _fail(out: TextIO, as_json: bool, exc: HostError, exit_code: int | None = None, *, plain: bool = False) -> int:
    if as_json:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
    elif plain:
        out.write(f"orch sign: {exc.message}\n")
    else:
        out.write(f"orch sign: {exc.code}: {exc.message}\n")
    return exit_code if exit_code is not None else exc.exit
