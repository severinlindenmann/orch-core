"""The author's side of the marketplace (V3 G5, #923; spec section 3 for the entry, section 5.1.1 for the digest).

``scaffold`` makes the files of a new add-on that passes the validator. ``pack`` reads a COMMIT of a local repository
through ``orch.gitsafe.market_local_export`` (the same plumbing-only export an install uses, never the working tree) and
returns the digest and the index entry to paste, after the checks an install runs on the package bytes (steps 12 to 16).
Nothing here talks to a host, signs, or writes; the CLI (``orch addon new | pack``) prints and does the one write."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from orch import __version__ as CORE_VERSION
from orch import gitsafe
from orch.addons import package
from orch.addons.manifest import CAPABILITIES, Manifest, load_manifest
from orch.market import MarketError
from orch.market import install as mi
from orch.market import url as market_url

__all__ = ["AuthorError", "Packed", "check_files", "index_entry", "pack", "scaffold"]

#: where an entry points when the author does not say; the schema accepts it, and ``pack`` says it must be replaced
PLACEHOLDER_GIT = "https://github.com/your-name/your-addon-repo"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}(?![\s\S])")


class AuthorError(Exception):
    """A refusal with a short fixed-shape text for the author (their own files, so the text may name a path or rule)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def scaffold(name: str) -> dict[str, bytes]:
    """The files of a new add-on called ``name``: a manifest (``orch.addon/3``, no capabilities, one field) and a main
    file that answers the runner's one JSON line. The result passes :func:`check_files`."""
    if _NAME.fullmatch(name) is None:
        raise AuthorError(
            "invalid.input", "the name is a-z, 0-9 and '-', starting with a letter, at most 40 characters"
        )
    title = name.replace("-", " ").title()
    manifest = {
        "schema": "orch.addon/3", "name": name, "version": "0.1.0", "title": title, "api": ">=1.0,<2",
        "min_core": CORE_VERSION, "capabilities": [], "entry": {"cmd": ["python3", "{pkg}/main.py"]},
        "fields": {"note": {"type": "string", "max_len": 200, "set_by": ["addon", "member"]}},
        "agents_md": f"{title}: the field {name}.note is a one-line note this add-on proposes.",
    }  # fmt: skip
    main = f'''"""orch add-on {name}: one JSON-RPC request on stdin, one response on stdout.
The host sends the ticket's key, type, status and this add-on's own fields. The add-on holds no key: it proposes,
the host checks the proposal against the grant and signs it itself."""
import json
import sys


def propose(params):
    ticket = params["ticket"]
    if ticket["status"] != "done" or ticket["fields"].get("note"):
        return {{}}  # nothing to propose
    return {{"set": {{"note": "Done: " + ticket["key"]}}}}


request = json.loads(sys.stdin.readline())
if request["method"] == "propose":
    reply = {{"jsonrpc": "2.0", "id": request["id"], "result": propose(request["params"])}}
else:
    reply = {{"jsonrpc": "2.0", "id": request["id"], "error": {{"code": -32601, "message": "no such method"}}}}
sys.stdout.write(json.dumps(reply) + "\\n")
'''
    readme = f"# {title}\n\nAn orch add-on. Edit `orch-addon.json` and `main.py`, commit, then `orch addon pack`.\n"
    files = {
        "orch-addon.json": (json.dumps(manifest, indent=2, sort_keys=False) + "\n").encode(),
        "main.py": main.encode(),
        "README.md": readme.encode(),
    }
    check_files(files)
    return files


def _manifest_entry(manifest: Manifest, pkg: package.Package) -> dict[str, Any]:
    doc = manifest.doc
    entry: dict[str, Any] = {
        "id": manifest.name, "version": manifest.version, "capabilities": manifest.capabilities,
        "min_orch": doc.get("min_core"), "package_sha256": pkg.digest,
    }  # fmt: skip
    return entry


def check_files(files: dict[str, bytes]) -> tuple[package.Package, Manifest]:
    """The checks an install runs on the bytes (spec steps 12, 14 and 16), against an entry made from the manifest
    itself: the package rules of 8.1, ``orch.addon/3`` (a marketplace add-on needs ``min_core``), a manifest that fits
    this core, no ``runtime`` key (not understood yet). Raises :class:`AuthorError`."""
    try:
        pkg = package.from_files(files)
    except package.PackageError as exc:
        raise AuthorError("market.package_invalid", str(exc)) from None
    try:
        manifest = load_manifest(pkg.files[package.MANIFEST_FILE])
    except Exception as exc:  # noqa: BLE001 - ManifestError, schema, strict JSON: all the author's file
        raise AuthorError("market.manifest_mismatch", f"orch-addon.json: {exc}"[:300]) from None
    if manifest.doc["schema"] != "orch.addon/3" or manifest.min_core is None:
        raise AuthorError(
            "market.manifest_mismatch", 'a marketplace add-on needs "schema": "orch.addon/3" with api and min_core'
        )
    unknown = [c for c in manifest.capabilities if c not in CAPABILITIES]
    if unknown:
        raise AuthorError("market.manifest_mismatch", f"unknown capabilities: {', '.join(unknown)}")
    try:
        mi.check_package(mi.Facts(_manifest_entry(manifest, pkg), "unsigned", None, None), pkg, CORE_VERSION)
    except MarketError as exc:
        raise AuthorError(exc.code, "the manifest does not pass the install checks (see the add-on API)") from None
    return pkg, manifest


def index_entry(
    manifest: Manifest, pkg: package.Package, *, git: str, commit: str, subdir: str | None
) -> dict[str, Any]:
    """The index entry (spec section 3) for ``manifest`` as committed: validated against the marketplace schema's
    ``entry`` definition. The ``publisher`` block is the marketplace owner's to add (it signs for one marketplace)."""
    source = {"git": git, **({"subdir": subdir} if subdir else {})}
    entry = {
        "id": manifest.name, "version": manifest.version, "source": source,
        "commit": commit, "package_sha256": pkg.digest, "capabilities": manifest.capabilities,
        "min_orch": manifest.doc["min_core"],
    }  # fmt: skip
    try:
        market_url.canonical_market_url(git)
    except MarketError:
        raise AuthorError("invalid.input", "--git is the https URL of the add-on repo (no .git, no port)") from None
    schema = market_url.schema()
    check = {"$ref": "#/$defs/entry", "$defs": schema["$defs"]}
    if next(Draft202012Validator(check).iter_errors(entry), None) is not None:
        raise AuthorError("market.malformed", "the entry does not fit the marketplace schema")
    return entry


@dataclass(frozen=True)
class Packed:
    commit: str
    digest: str
    entry: dict[str, Any]
    placeholder_git: bool
    files: dict[str, bytes]


def pack(repo: str, rev: str | None = None, *, subdir: str | None = None, git: str | None = None) -> Packed:
    """Validate the package at commit ``rev`` (default ``HEAD``) of the local repo ``repo``; build its entry."""
    try:
        commit, files = gitsafe.market_local_export(repo, rev, subdir)
    except gitsafe.MarketGitError as exc:
        if exc.kind == "package":
            raise AuthorError(
                "market.package_invalid",
                "the commit's files break a package rule (8.1): no links, "
                "submodules, hidden or non-ASCII names, binaries or oversize files",
            ) from None
        raise AuthorError("market.fetch_failed", "that commit cannot be read from the repository") from None
    except gitsafe.Unsafe as exc:
        raise AuthorError("invalid.input", str(exc)[:300]) from None
    pkg, manifest = check_files(files)
    entry = index_entry(manifest, pkg, git=git or PLACEHOLDER_GIT, commit=commit, subdir=subdir)
    return Packed(commit, pkg.digest, entry, git is None, files)
