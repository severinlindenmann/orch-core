"""``orch addon install | update | remove``: add-ons from a marketplace (V3 G3, #921; spec sections 5 to 7), and the
author tools ``new | pack | dev`` (V3 G5, #923; spec 5.1.1 "Digest of the author's working tree").

    orch addon install ID [--from URL] [--workspace W] [--json]   fetch, review, sign: the add-on is installed
    orch addon update NAME [--workspace W] [--json]               the same for a newer entry of its origin
    orch addon update [NAME] --check [--json]                     what is installed and what is newer (read only)
    orch addon remove NAME [--workspace W] [--json]               turn it off (signed) and delete the package
    orch addon new NAME [--json]                                  scaffold ./NAME (a manifest and a main file)
    orch addon pack [PATH] [--commit REV] [--subdir D] [--git URL] [--json]
                                                                  validate a commit, print digest and index entry
    orch addon dev PATH [--commit REV] [--subdir D] [--workspace W] [--json]
                                                                  try a local commit in a DEV workspace (not an install)
    orch addon setup NAME [--workspace W] [--json]                (re)install the add-on's dependencies (G4)

Everything except ``update --check`` is a person's command: with ``ORCH_GRANT`` or ``ORCH_SESSION`` set it is refused
(``market.actor_refused``) before the host is asked, so nothing is fetched on an agent's behalf; the host refuses an
agent again. ``install`` and ``update`` end in the existing ``addon.grant`` review and signature at the person's
terminal; the package is the one the host staged, never a folder. ``remove`` signs ``addon.disable`` when the add-on is
on and then asks the host to delete the package; data stays until ``orch addon purge``. An add-on that declares
dependencies (``runtime``) gets them installed by the host right after the signature (``market.env_build``);
``setup`` runs that step again (a retry, a changed environment, a new machine).

``new`` and ``pack`` are local and offline (no host, no key, nothing signed), so an agent may run them. ``pack`` hashes
a COMMIT of a local git repository with the same closed export an install uses, never the working tree, so its digest is
the digest of an install of that commit. ``dev`` is a person's command like ``install``; the host accepts it only for a
dev sandbox workspace on a dev host (``host.dev_refused`` everywhere else), signs through the same ``addon.grant``
review, and records no install."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.market import ADDONS  # the store-layout lint matches the bare word

__all__ = ["main"]

_USAGE = (
    "usage: orch addon {install ID [--from URL] | update NAME | update [NAME] --check | remove NAME | setup NAME | "
    "new NAME | pack [PATH] [--commit REV] [--subdir D] [--git URL] | dev PATH [--commit REV] [--subdir D]} "
    "[--workspace W] [--json]"
)


class _Quiet(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # type: ignore[override]
        raise _Fail("invalid.input", f"{message}\n{_USAGE}", 2)


class _Fail(Exception):
    def __init__(self, code: str, message: str, exit_: int = 3) -> None:
        super().__init__(message)
        self.code, self.message, self.exit = code, message, exit_


def _parser() -> argparse.ArgumentParser:
    p = _Quiet(prog="orch addon", add_help=False)
    p.add_argument("cmd", choices=("install", "update", "remove", "setup", "new", "pack", "dev"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--from", dest="url")
    p.add_argument("--check", action="store_true")
    p.add_argument("--commit")
    p.add_argument("--subdir")
    p.add_argument("--git")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    p.add_argument("--state", type=Path)
    p.add_argument("--run", type=Path)
    return p


def _lines(stage: dict[str, Any]) -> list[str]:
    market = stage["market"]
    pub = market["publisher"]
    who = {
        "unsigned": "unsigned",
        "new": "first use (remembered when you sign)",
        "known": "same publisher as before",
        "pinned": "key pinned by your hub",
    }
    out = [f"{stage['name']} {stage['version']}  package {stage['package_sha256']}", *stage["review"]]
    note = "(per host, not signed)"
    out += [
        f"marketplace {market['url']} ({market['class']}), index v{market['index_version']}"
        + (" STALE" if market["stale"] else "")
        + f" {note}",
        f"commit {market['commit']}, {market['source_note']} {note}",
        f"publisher {pub['fingerprint'] or 'none'}: {who[pub['tofu']]} {note}",
    ]
    if market["summary"]:
        out.append(f"summary from the marketplace: {market['summary']} {note}")
    return out


def _installed_text(rows: list[dict[str, Any]], hub: dict[str, Any] | None = None) -> str:
    out = []
    if hub and hub.get("state") == "unverified":  # the cause of a pause that has no policy behind it, and the way out
        out.append(
            "hub policy cannot be verified: every add-on is paused (the hub card expired, or the hub is unreachable); "
            f"reconnect to the hub: {hub['reconnect']}"
        )
    if not rows:
        return "\n".join([*out, "no add-ons installed from a marketplace"])
    for r in rows:
        line = f"{r['name']} {r['version']} [{r['state']}] from {r['origin']}"
        a = r["available"]
        if a:
            added = f" (adds {', '.join(a['capabilities_added'])})" if a["capabilities_added"] else ""
            line += "  REVOKED" if a["revoked"] else f"  newer: {a['version']}{added}"
        p = r.get("paused")
        if p:
            line += f"  PAUSED by hub policy ({p['code']})"
            line += "; reconnect to the hub: " + p["reconnect"] if p["reconnect"] else "; ask the hub admin"
        out.append(line)
    return "\n".join(out)


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: Any = None,
) -> int:
    out = out if out is not None else sys.stdout
    as_json = "--json" in argv

    def fail(exc: _Fail) -> int:
        if as_json:
            out.write(json.dumps({"ok": False, "code": exc.code, "message": exc.message}) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n")
        return exc.exit

    from orch.host.errors import HostError
    from orch.ops.errors import ERRORS

    try:
        if "--help" in argv or "-h" in argv:  # usage is not a refusal
            out.write(_USAGE + "\n\n" + (__doc__ or "").split("\n\n", 1)[0] + "\n")
            return 0
        a = _parser().parse_intermixed_args(argv)
        want = {
            "install": (1, 1), "update": (0, 1), "remove": (1, 1), "setup": (1, 1), "new": (1, 1), "pack": (0, 1),
            "dev": (1, 1),
        }[a.cmd]  # fmt: skip
        if not want[0] <= len(a.rest) <= want[1] or (a.cmd == "update" and not a.check and not a.rest):
            raise _Fail("invalid.input", _USAGE, 2)
        if (a.check and a.cmd != "update") or (a.url and a.cmd != "install"):
            raise _Fail("invalid.input", _USAGE, 2)
        if (a.commit or a.subdir) and a.cmd not in ("pack", "dev") or (a.git and a.cmd != "pack"):
            raise _Fail("invalid.input", _USAGE, 2)
        if a.cmd in ("new", "pack"):  # local and offline: no host, no key, nothing signed
            data = _new(a) if a.cmd == "new" else _pack(a)
            out.write(
                json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n" if as_json else _author_text(a, data)
            )
            return 0
        agent = bool(env.get("ORCH_GRANT") or env.get("ORCH_SESSION"))
        if agent and not a.check:  # before the host is asked: nothing is fetched on an agent's behalf
            raise _Fail("market.actor_refused", ERRORS["market.actor_refused"].message)
        if a.cmd == "dev":  # a bad path is the author's mistake, said before the host is connected
            a.rest[0] = _repo_root(a.rest[0])
        if a.check:
            data = _check(a, env)
            out.write(
                json.dumps({"ok": True, "data": data}) + "\n"
                if as_json
                else "ok addon update --check\n" + _installed_text(data[ADDONS], data.get("hub")) + "\n"
            )
            return 0
        result = _signed(a, env, out, as_json)
        out.write(json.dumps({"ok": True, "data": result}, sort_keys=True) + "\n" if as_json else _done(a, result))
        return 0
    except _Fail as exc:
        return fail(exc)
    except HostError as exc:
        return fail(_Fail(exc.code, exc.message, exc.exit))
    except (OSError, ConnectionError) as exc:
        return fail(_Fail("host.unavailable", str(exc)[:300], 9))


def _new(a: argparse.Namespace) -> dict[str, Any]:
    """Write the scaffold into ``./NAME`` (a new directory: an existing one is refused, nothing is overwritten)."""
    from orch.market import author

    name = a.rest[0]
    files = _author(author.scaffold, name)
    target = Path.cwd() / name
    made = False
    try:
        os.mkdir(target, 0o755)
        made = True
        # open the new directory ONCE, without following a link, and create every file through that descriptor: a
        # swap of ./NAME for a link after the mkdir is never followed
        dir_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for rel, data in sorted(files.items()):
                fd = os.open(rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
        except OSError:
            for rel in sorted(files):  # only what this call created, by name below its own directory descriptor
                with contextlib.suppress(OSError):
                    os.unlink(rel, dir_fd=dir_fd)
            raise
        finally:
            os.close(dir_fd)
    except FileExistsError:
        raise _Fail("invalid.input", f"{target} already exists; nothing was written", 2) from None
    except OSError as exc:
        if made:
            with contextlib.suppress(OSError):
                os.rmdir(target)  # empty again, or not ours any more: rmdir never removes a non-empty directory
        raise _Fail("invalid.input", f"cannot write {target}: {exc.strerror}; nothing was kept", 2) from None
    return {"name": name, "path": str(target), "files": sorted(files)}


def _author(fn: Any, *args: Any, **kw: Any) -> Any:
    from orch.market import author

    try:
        return fn(*args, **kw)
    except author.AuthorError as exc:
        raise _Fail(exc.code, exc.message, 2 if exc.code == "invalid.input" else 3) from None


def _repo_root(raw: str | None) -> str:
    """The repository directory named by the author: absolute, a real directory, the ROOT of a git repository (a package
    inside one is ``--subdir``, so the digest is computed from the same tree an install reads)."""
    path = os.path.realpath(raw or os.getcwd())
    if not os.path.isdir(path):
        raise _Fail("invalid.input", f"{path} is not a directory", 2)
    if not os.path.lexists(os.path.join(path, ".git")):
        raise _Fail(
            "invalid.input",
            f"{path} is not the root of a git repository (commit the add-on first; a package inside one is --subdir)",
            2,
        )
    return path


def _pack(a: argparse.Namespace) -> dict[str, Any]:
    from orch.market import author

    packed = _author(author.pack, _repo_root(a.rest[0] if a.rest else None), a.commit, subdir=a.subdir, git=a.git)
    return {
        "name": packed.entry["id"], "version": packed.entry["version"], "commit": packed.commit,
        "package_sha256": packed.digest, "entry": packed.entry, "placeholder_git": packed.placeholder_git,
        "warnings": list(packed.warnings),
    }  # fmt: skip


def _author_text(a: argparse.Namespace, data: dict[str, Any]) -> str:
    if a.cmd == "new":
        return (
            f"ok addon new {data['name']}: wrote {', '.join(data['files'])} to {data['path']}\n"
            f"next: cd {data['name']} && git init && git add -A && git commit -m first && orch addon pack\n"
        )
    lines = [
        f"ok addon pack {data['name']} {data['version']}",
        f"commit {data['commit']}",
        f"package_sha256 {data['package_sha256']}",
        'index entry (paste into the "addons" list of your marketplace.json):',
        json.dumps(data["entry"], indent=2, sort_keys=True),
    ]
    if data["placeholder_git"]:
        lines.append('note: "source.git" is a placeholder; pass --git https://github.com/<you>/<repo> or edit it')
    lines += [f"warning: {w}" for w in data.get("warnings", [])]
    lines.append(
        "note: the commit is what installs; push it so the marketplace can fetch it. Uncommitted files are not in it."
    )
    return "\n".join(lines) + "\n"


def _done(a: argparse.Namespace, result: dict[str, Any]) -> str:
    if a.cmd == "dev":
        st = result["stage"]
        return (
            f"ok addon dev {st['name']} {st['version']}: signed and running from {st['commit'][:12]} "
            "(a dev add-on, not an install: re-run orch addon dev after a change)\nnext: orch addon list\n"
        )
    if result.get("up_to_date"):
        return f"ok addon update {result['name']}: already up to date ({result['version']})\n"
    if a.cmd == "remove":
        return f"ok addon remove {result['name']}: package deleted; data stays (orch addon purge {result['name']})\n"
    if a.cmd == "setup":
        return f"ok addon setup {result['name']}: {result['count']} files of dependencies installed and checked\n"
    st = result["stage"]
    deps = result.get("env")
    more = f"; dependencies installed ({deps['count']} files)" if deps else ""
    return f"ok addon {a.cmd} {st['name']} {st['version']}: signed and installed{more}\nnext: orch addon list\n"


def _check(a: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
    from orch.cli.publish import PubError, _call, _target

    try:
        data = _call(_target(env), "market.installed", {}, env)
    except PubError as exc:
        raise _Fail(exc.code, exc.message, exc.exit) from None
    rows = data.get(ADDONS, [])
    if a.rest:
        rows = [r for r in rows if r["name"] == a.rest[0]]
        if not rows:
            raise _Fail("market.not_installed", "this add-on was not installed from a marketplace")
    return {ADDONS: rows}


def _connect(a: argparse.Namespace, env: Mapping[str, str]) -> Any:
    """``(conn, backend, device, wid, person, name_prefix)`` for the one workspace of this person (as ``orch sign``)."""
    from orch.host import handoff
    from orch.host.client import Connection, Pin
    from orch.identity import device_ref
    from orch.ops import human
    from orch.ops.runtime import state_dir

    state = a.state.resolve() if a.state is not None else state_dir(env)
    uid = os.getuid()
    entries = handoff._own_workspaces(state, uid, a.workspace)  # noqa: SLF001
    if len(entries) != 1:
        raise _Fail("invalid.input", "name the workspace with --workspace (none or several map you to a person)", 2)
    entry = entries[0]
    wid, person = entry["workspace_id"], entry["peers"][str(uid)]["person"]
    backend = handoff._backend(state, wid)  # noqa: SLF001
    device = device_ref(backend.public_key(human.KEY_ID))
    path = handoff._socket(a.run, state, int(entry["host_uid"]), wid)  # noqa: SLF001
    conn = Connection(Pin(int(entry["host_uid"]), wid, path), client="orch addon")
    label = (Path(str(entry.get("root") or wid)).name, str(entry.get("prefix")))
    return conn, backend, device, wid, person, label


def _sign(conn: Any, backend: Any, device: str, wid: str, person: str, label: Any, op: str, args: dict) -> Any:
    from orch.host import handoff, signer
    from orch.host.control_client import new_idem
    from orch.ops import human

    prepared = conn.call("human.prepare", {"op": op, "args": args, "signer": {"device": device, "auth": backend.auth}})[
        "data"
    ]
    return signer.finish(
        conn.call, prepared, prepared["request"]["id"], backend=backend, key_id=human.KEY_ID, workspace_id=wid,
        person=person, device=device, workspace=lambda w: label if w == wid else None, confirm=human.review_prompt,
        show_secret=human.show_secret, idem=new_idem(), prefix=f"{handoff.NOTICE}\n\n",
    )  # fmt: skip


def _signed(a: argparse.Namespace, env: Mapping[str, str], out: TextIO, as_json: bool) -> dict[str, Any]:
    from orch.host import signer

    conn = None
    try:
        conn, backend, device, wid, person, label = _connect(a, env)
        if a.cmd == "remove":
            name = a.rest[0]
            rows = conn.call("market.installed", {})["data"][ADDONS]
            row = next((r for r in rows if r["name"] == name), None)
            if row is None:
                raise _Fail("market.not_installed", "this add-on was not installed from a marketplace")
            if row["state"] == "enabled":
                _sign(conn, backend, device, wid, person, label, "addon.disable", {"name": name})
            return conn.call("market.uninstall", {"name": name})["data"]
        if a.cmd == "setup":  # the host installs the dependencies of the granted package again
            return conn.call("market.env_build", {"name": a.rest[0]}, timeout=900.0)["data"]
        if a.cmd == "dev":
            dev: dict[str, Any] = {"path": a.rest[0]}  # resolved once, in main()
            if a.commit:
                dev["rev"] = a.commit
            if a.subdir:
                dev["subdir"] = a.subdir
            stage = conn.call("market.dev_stage", dev, timeout=120.0)["data"]
            if not as_json:
                out.write(f"DEV add-on from a local folder, not from a marketplace (commit {stage['commit']})\n")
                out.write(
                    "\n".join(
                        [f"{stage['name']} {stage['version']}  package {stage['package_sha256']}", *stage["review"]]
                    )
                )
                out.write("\n")
            _sign(
                conn,
                backend,
                device,
                wid,
                person,
                label,
                "addon.grant",
                {"name": stage["name"], "staged": stage["stage"]},
            )
            return {"stage": stage, "installed": False}
        args: dict[str, Any] = {"id": a.rest[0]}
        if a.cmd == "update":
            args["update"] = True
        if a.url:
            args["url"] = a.url
        stage = conn.call("market.stage", args, timeout=300.0)["data"]
        if stage.get("up_to_date"):
            return stage
        if not as_json:
            out.write("\n".join(_lines(stage)) + "\n")
        _sign(
            conn, backend, device, wid, person, label, "addon.grant", {"name": stage["name"], "staged": stage["stage"]}
        )
        result: dict[str, Any] = {"stage": stage, "installed": True}
        if stage["grant"].get("runtime") is not None:  # the dependencies the signature promised, now that it exists
            result["env"] = conn.call("market.env_build", {"name": stage["name"]}, timeout=900.0)["data"]
        return result
    except signer.SignerRefused as exc:
        raise _Fail("invalid.input", f"{exc}; nothing was signed", 2) from None
    finally:
        if conn is not None:
            conn.close()
