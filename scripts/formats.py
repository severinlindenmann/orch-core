#!/usr/bin/env python3
"""Persisted-format guard: a change to what orch writes to disk cannot merge without a migration step.

Per root kind (host, workspace, hub, relay, publish) :func:`compute` reads the sources that define the persisted
format and returns ``{"kind", "format", "fingerprint", "sources"}``:

* ``format`` is ``CURRENT[kind]``, parsed from the TEXT of ``packages/core/src/orch/migrate/steps.py`` (no import of
  core: hub, relay and publish do not depend on it);
* ``fingerprint`` is the sha256 of a canonical description of the sources (see :data:`RULES`): SQL DDL with comments
  stripped and whitespace collapsed, version/schema constants and the version keys of persisted JSON as written in the
  code, and sorted lists of file and directory names. Python files are read with :mod:`tokenize` (no import, no
  ``ast``: the hub is written for a newer Python than this script has to run under), so comments, docstrings and
  formatting never change a fingerprint;
* ``sources`` is the list of repo paths that contributed.

``--check [kind...]`` compares with ``contracts/formats/<kind>.json``; ``--update [kind...]`` rewrites it. Every source
is read through :func:`read_source` (tests replace it to simulate an edit).
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import hashlib
import io
import json
import re
import sys
import tokenize
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = "contracts/formats"
STEPS = "packages/core/src/orch/migrate/steps.py"
STEPS_GLOB = "packages/core/src/orch/migrate/**/*.py"
KINDS = ("host", "workspace", "hub", "relay", "publish")
#: bump when the extraction itself changes (every contract is then regenerated with ``--update --allow-same-format``)
SCHEME = 1

CORE = "packages/core/src/orch/"
HUB = "packages/hub/src/orch_hub/"
RELAY = "packages/relay/src/orch_relay/"
PUBLISH = "packages/publish/pubsrv/"

#: Core files that write the WORKSPACE store (``<project>/.orch``: layout, config, event logs, checkpoints, the derived
#: index) or build signed/chained shapes that live in it (events, membership, cards, genesis). Everything else in core
#: that persists belongs to the HOST kind (the host state dir: ``host/``, ``hosts/<ws>/``, custody, click, runner...).
WORKSPACE_FILES = [
    CORE + "layout.py",
    CORE + "discovery.py",
    CORE + "canon/hashing.py",
    CORE + "store/*.py",
    CORE + "identity/certs.py",
    CORE + "identity/genesispin.py",
    CORE + "identity/memberlist.py",
    CORE + "identity/wscard.py",
    CORE + "ops/*.py",
    CORE + "records/*.py",
    CORE + "importer/plan.py",
    CORE + "host/sequence.py",
    CORE + "host/seqaddon.py",
    CORE + "host/seqgates.py",
    CORE + "host/seqquick.py",
    CORE + "host/hubdeliver.py",
    CORE + "host/human.py",
]
#: ``store/pins.py`` keeps ``<host state>/hosts/<ws>/heads.sqlite``: host state, not the store
WORKSPACE_EXCLUDE = [CORE + "store/pins.py"]
HOST_FILES = [
    CORE + "addons/*.py",
    CORE + "click/*.py",
    CORE + "custody/*.py",
    CORE + "devsandbox.py",
    CORE + "gitread/*.py",
    CORE + "host/**/*.py",
    CORE + "identity/*.py",
    CORE + "install/*.py",
    CORE + "lifecycle/*.py",
    CORE + "migrate/backup.py",
    CORE + "migrate/format.py",
    CORE + "migrate/journal.py",
    CORE + "prune/*.py",
    CORE + "runner/*.py",
    CORE + "setup/review.py",
    CORE + "hubclient/netconfig.py",
    CORE + "store/pins.py",
]
#: what the host rules leave to the workspace (``store/*.py`` is not in :data:`HOST_FILES`, ``pins.py`` is named there)
HOST_EXCLUDE = [p for p in WORKSPACE_FILES if p != CORE + "store/*.py"]

LAYOUT_NAMES = ["STORE_DIR", "KEYS_FILE", "WORK_DIR", "STATE_DIR", "CONFIG_SCHEMA", "_LEGACY_NAMES"]
HUB_SIGNED = [
    HUB + "keys/store.py",
    HUB + "audit/log.py",
    HUB + "access/snapshots.py",
    HUB + "backup/service.py",
    HUB + "backup/restore.py",
    HUB + "closing.py",
]
BACKUP_NAMES = [
    "KIND",
    "MAGIC",
    "REQUIRED_FILES",
    "EXCLUDED_NAMES",
    "EXCLUDED_DIRS",
    "EXCLUDED_SUFFIXES",
    "SEQ_FIELDS",
    "HEADER_FIELDS",
]

#: Per kind a list of rules. ``include``/``exclude``: repo-relative globs (``fnmatch`` on the posix path, ``**`` through
#: ``Path.glob``); ``extract``: what to read from each file (``ddl``, ``consts``, ``vlits``, ``names``, ``layout``);
#: ``named``: ``{path: [constant names]}`` taken as written, whatever their name.
RULES: dict[str, list[dict[str, Any]]] = {
    # --- core: the host state dir -------------------------------------------------------------------------------
    "host": [
        # every SQLite DDL in core that is not a workspace-store file (a new DDL file anywhere is seen)
        {"include": [CORE + "**/*.py"], "exclude": WORKSPACE_FILES + [CORE + "migrate/*"], "extract": ["ddl"]},
        {"include": [CORE + "store/pins.py"], "exclude": [], "extract": ["ddl"]},
        {"include": HOST_FILES, "exclude": HOST_EXCLUDE, "extract": ["consts", "vlits"]},
    ],
    # --- core: the workspace store ------------------------------------------------------------------------------
    "workspace": [
        {"include": WORKSPACE_FILES, "exclude": WORKSPACE_EXCLUDE, "extract": ["ddl", "consts", "vlits"]},
        # the store layout names
        {"named": {CORE + "layout.py": LAYOUT_NAMES}},
    ],
    # --- hub: the state dir -------------------------------------------------------------------------------------
    "hub": [
        {"include": [HUB + "**/*.py"], "exclude": [], "extract": ["ddl"]},
        # the version constants (``settings.VERSION``) and the file-name constants of the state dir
        {"include": [HUB + "**/*.py"], "exclude": [HUB + "contract/*", HUB + "versions.py"], "extract": ["consts"]},
        # names of the files in the state dir (``meta.json``, ``*.db``, key files): names only
        {"include": [HUB + "**/*.py"], "exclude": [HUB + "contract/*"], "extract": ["names"]},
        # the version keys of the signed or sealed objects the hub writes
        {"include": HUB_SIGNED, "exclude": [], "extract": ["vlits"]},
        # the backup format (what a backup lists and refuses to list)
        {"named": {HUB + "contract/backup.py": BACKUP_NAMES}},
    ],
    # --- relay: relay.sqlite3 -----------------------------------------------------------------------------------
    "relay": [
        {"include": [RELAY + "*.sql", RELAY + "*.py"], "exclude": [], "extract": ["ddl"]},
        {"include": [RELAY + "db.py"], "exclude": [], "extract": ["consts"]},
    ],
    # --- publish: publish.db and the data dir layout --------------------------------------------------------------
    "publish": [
        {"include": [PUBLISH + "*.py"], "exclude": [], "extract": ["ddl", "layout"]},
        {"include": [PUBLISH + "db.py"], "exclude": [], "extract": ["consts"]},
    ],
}

CHANGED = (
    "persisted format changed for {kind}: add a migration step (packages/core/src/orch/migrate/steps.py), "
    'bump CURRENT["{kind}"] and run scripts/formats.py --update {kind}'
)


# --- reading sources ---------------------------------------------------------------------------------------


def read_source(rel: str) -> str:
    """The text of a repo path (the one reader every extraction goes through)."""
    return (ROOT / rel).read_text(encoding="utf-8")


def list_sources(pattern: str) -> list[str]:
    """Repo paths below the root that match a glob, sorted."""
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in ROOT.glob(pattern)
        if p.is_file() and "__pycache__" not in p.parts and "node_modules" not in p.parts
    )


def _expand(include: list[str], exclude: list[str]) -> list[str]:
    seen: set[str] = set()
    for pattern in include:
        seen.update(list_sources(pattern))
    return sorted(p for p in seen if not any(fnmatch.fnmatchcase(p, e) for e in exclude))


# --- tokens ------------------------------------------------------------------------------------------------

#: ``(type, text, column, first_on_line)``; type is NAME, NUM, OP, STR or NEWLINE
Item = tuple[str, str, int, bool]
_SKIP = {tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
_STRING = re.compile(r"^([A-Za-z]*)(\"{3}|'{3}|\"|')(.*)\2$", re.S)
_FIELD = re.compile(r"\{[^{}]*\}")


def _string_body(token: str) -> str:
    m = _STRING.match(token)
    if not m:
        return token
    body = m.group(3)
    if "f" in m.group(1).lower():
        body = _FIELD.sub("{}", body)  # a replacement field is one ``{}`` on every Python
    return body


def lex(text: str) -> list[Item]:
    """The significant tokens of a Python file (cached: the same text is read by several rules; callers never change
    the list). Adjacent string literals are one ``STR`` (implicit concatenation); an f-string is one ``STR`` with ``{}``
    for each replacement field (3.12+ splits them into several tokens)."""
    return list(_lex(text))


@functools.lru_cache(maxsize=16)
def _lex(text: str) -> tuple[Item, ...]:
    fstart = getattr(tokenize, "FSTRING_START", None)
    fmid = getattr(tokenize, "FSTRING_MIDDLE", None)
    fend = getattr(tokenize, "FSTRING_END", None)
    items: list[Item] = []
    stack: list[list[Any]] = []  # [parts, expression depth, column, first]
    first = True
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        tt, s, col = tok[0], tok[1], tok[2][1]
        if tt in _SKIP:
            continue
        if fstart is not None and tt == fstart:
            stack.append([[], 0, col, first])
            first = False
            continue
        if stack:
            top = stack[-1]
            if tt == fmid:
                if top[1] == 0:
                    top[0].append(s)
            elif tt == fend:
                stack.pop()
                if not stack:
                    items.append(("STR", "".join(top[0]), top[2], top[3]))
            elif tt == tokenize.OP and s == "{":
                if top[1] == 0:
                    top[0].append("{}")
                top[1] += 1
            elif tt == tokenize.OP and s == "}":
                top[1] -= 1
            continue
        if tt == tokenize.NEWLINE:
            items.append(("NEWLINE", "", 0, False))
            first = True
            continue
        if tt == tokenize.NAME:
            items.append(("NAME", s, col, first))
        elif tt == tokenize.NUMBER:
            items.append(("NUM", s, col, first))
        elif tt == tokenize.OP:
            items.append(("OP", s, col, first))
        elif tt == tokenize.STRING:
            items.append(("STR", _string_body(s), col, first))
        else:
            continue
        first = False
    merged: list[Item] = []
    for it in items:
        if it[0] == "STR" and merged and merged[-1][0] == "STR":
            prev = merged[-1]
            merged[-1] = ("STR", prev[1] + it[1], prev[2], prev[3])
        else:
            merged.append(it)
    return tuple(merged)


_OPEN, _CLOSE = {"(", "[", "{"}, {")", "]", "}"}


def _top_assigns(items: list[Item]) -> list[tuple[list[str], list[Item]]]:
    """``[(target names, right-hand tokens)]`` of the module-level assignments (``NAME = ...``, ``NAME: T = ...``,
    ``A, B = ...``)."""
    out: list[tuple[list[str], list[Item]]] = []
    n = len(items)
    i = 0
    while i < n:
        end = i
        while end < n and items[end][0] != "NEWLINE":
            end += 1
        t, _, col, first = items[i]
        if t == "NAME" and first and col == 0:
            depth = 0
            eq = None
            for j in range(i, end):
                tt, ss = items[j][0], items[j][1]
                if tt == "OP":
                    if ss in _OPEN:
                        depth += 1
                    elif ss in _CLOSE:
                        depth -= 1
                    elif ss == "=" and depth == 0:
                        eq = j
                        break
            if eq is not None:
                names: list[str] = []
                j = i
                ok = True
                while j < eq:
                    if items[j][0] != "NAME":
                        ok = False
                        break
                    names.append(items[j][1])
                    j += 1
                    if j < eq and items[j][:2] == ("OP", ","):
                        j += 1
                        continue
                    break
                if ok and j < eq and items[j][:2] != ("OP", ":"):
                    ok = False
                if ok and names:
                    out.append((names, items[eq + 1 : end]))
        i = end + 1
    return out


def _split_commas(rhs: list[Item]) -> list[list[Item]]:
    parts: list[list[Item]] = [[]]
    depth = 0
    for it in rhs:
        if it[0] == "OP" and it[1] in _OPEN:
            depth += 1
        elif it[0] == "OP" and it[1] in _CLOSE:
            depth -= 1
        if it[:2] == ("OP", ",") and depth == 0:
            parts.append([])
        else:
            parts[-1].append(it)
    if not parts[-1]:
        parts.pop()
    return parts


# --- normalisation -----------------------------------------------------------------------------------------

_DDL = re.compile(r"\b(?:CREATE|ALTER)\s+(?:UNIQUE\s+|VIRTUAL\s+|TEMP\s+|TEMPORARY\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b")
_USER_VERSION = re.compile(r"PRAGMA\s+user_version")


def norm_sql(sql: str) -> str:
    """Comments stripped, whitespace collapsed."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = sql.replace("\\n", " ").replace("\\t", " ")
    return re.sub(r"\s+", " ", sql).strip()


def _is_ddl(text: str) -> bool:
    return bool(_DDL.search(text) or _USER_VERSION.search(text))


_VERSION_NAME = re.compile(r"(?:^|_)(?:VERSION|V|SCHEMA)\d*$")
_FILE_VALUE = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:json|jsonl|sqlite3?|db|key|pem|secret|lock|log|used)")
_FILE_NAME = re.compile(r"(?:^|_)(?:FILE|DIR|MARK|MARKER|SUFFIX)$|^(?:NAME|FILE|DIR|SUFFIX)$")
_SIMPLE_NAMES = {"frozenset", "set", "tuple", "True", "False", "None"}
_SIMPLE_OPS = {",", "(", ")", "[", "]", "{", "}", ":", "-", "+", "|"}
_STATE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}\.(?:db|json|key|secret|used|lock|sock)")
_NAMEISH = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9_.-]*")
#: the calls whose string arguments name a file or folder
_LAYOUT_CALLS = {
    "join", "joinpath", "makedirs", "mkdir", "rename", "rmdir", "listdir", "scandir", "symlink", "_open_dir",
    "share_dir", "make_incoming", "write_file", "verify_file", "stat_files", "remove_tree",
}  # fmt: skip
#: version keys of persisted JSON; ``schema`` takes a string of the form ``orch.<name>/<n>``
_VERSION_KEYS = {"v", "hash_v", "roster_v", "suite", "version"}
_SCHEMA_VALUE = re.compile(r"orch\.[\w.-]+/\d+")


def _render(rhs: list[Item], *, simple_only: bool) -> str | None:
    """The right-hand side as one normalised string; ``None`` when ``simple_only`` and it is not a plain literal."""
    if simple_only:
        has_value = False
        for t, s, _, _ in rhs:
            if t in ("STR", "NUM"):
                has_value = True
            elif t == "NAME" and s in _SIMPLE_NAMES:
                continue
            elif t == "OP" and s in _SIMPLE_OPS:
                continue
            else:
                return None
        if not has_value:
            return None
    ops = {s for t, s, _, _ in rhs if t == "OP"}
    if "{" in ops and ":" not in ops and "[" not in ops and all(t in ("STR", "OP", "NAME") for t, *_ in rhs):
        return "set:" + json.dumps(sorted(s for t, s, _, _ in rhs if t == "STR"))  # a set has no order
    return " ".join(json.dumps(norm_sql(s) if _is_ddl(s) else s) if t == "STR" else s for t, s, _, _ in rhs)


# --- extractors --------------------------------------------------------------------------------------------


def _consts(items: list[Item], named: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for names, rhs in _top_assigns(items):
        values = [rhs] if len(names) == 1 else _split_commas(rhs)
        if len(values) != len(names):
            continue
        for name, value in zip(names, values, strict=True):
            if named is not None:
                if name in named:
                    out[name] = _render(value, simple_only=False) or ""
                continue
            rendered = _render(value, simple_only=True)
            if rendered is None:
                continue
            is_str = len(value) == 1 and value[0][0] == "STR"
            if is_str and _is_ddl(value[0][1]):
                continue  # a schema string: carried by ``ddl``
            if _VERSION_NAME.search(name) or (
                is_str and (_FILE_VALUE.fullmatch(value[0][1]) or _FILE_NAME.search(name))
            ):
                out[name] = rendered
    if named is not None:
        for name in named:
            out.setdefault(name, "<missing>")  # a renamed or removed constant is a change too
    return out


def _vlits(items: list[Item]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(len(items) - 2):
        t, s, *_ = items[i]
        if t != "STR" or items[i + 1][:2] != ("OP", ":"):
            continue
        vt, vs = items[i + 2][0], items[i + 2][1]
        if (s in _VERSION_KEYS and vt == "NUM") or (s == "schema" and vt == "STR" and _SCHEMA_VALUE.fullmatch(vs)):
            key = f'"{s}": {vs}' if vt == "NUM" else f'"{s}": "{vs}"'
            out[key] = out.get(key, 0) + 1
    return out


def _ddl(items: list[Item]) -> list[str]:
    """Every string literal that holds SQL DDL (or sets ``user_version``), normalised, sorted."""
    found = []
    for i, (t, s, _, _) in enumerate(items):
        if t == "STR" and _is_ddl(s) and not _is_docstring(items, i):
            found.append(norm_sql(s))
    return sorted(found)


def _names(items: list[Item]) -> list[str]:
    return sorted({s for t, s, *_ in items if t == "STR" and _STATE_NAME.fullmatch(s)})


def _after(items: list[Item], i: int, name: str) -> bool:
    """The token at ``i`` is an attribute of ``name`` (``name.<token>``)."""
    return i >= 2 and items[i - 1][:2] == ("OP", ".") and items[i - 2][:2] == ("NAME", name)


def _is_docstring(items: list[Item], i: int) -> bool:
    """A string that is a statement of its own (a docstring): never SQL that is run."""
    return items[i][3] and i + 1 < len(items) and items[i + 1][0] == "NEWLINE"


def _layout(items: list[Item]) -> list[str]:
    found: set[str] = set()
    n = len(items)

    def take(it: Item) -> None:
        if it[0] == "STR" and _NAMEISH.fullmatch(it[1]):
            found.add(it[1])

    for i, it in enumerate(items):
        if it[0] == "STR" and i and items[i - 1][:2] == ("OP", "/"):
            take(it)  # ``data_dir / "shares"``
        opens = i + 1 < n and items[i + 1][:2] == ("OP", "(")
        # ``os.path.join(...)`` only (not ``", ".join(...)``); the other names are calls of the data dir code
        is_call = opens and it[0] == "NAME" and it[1] in _LAYOUT_CALLS
        if it[1] == "join" and not _after(items, i, "path"):
            is_call = False
        # ``for kind in ("incoming", "shares"):``
        is_set = opens and it[:2] == ("NAME", "in") and i >= 2 and items[i - 2][:2] == ("NAME", "for")
        if not (is_call or is_set):
            continue
        depth = 0
        for j in range(i + 1, n):
            t, s = items[j][0], items[j][1]
            if t == "OP" and s in _OPEN:
                depth += 1
            elif t == "OP" and s in _CLOSE:
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and t == "STR":
                take(items[j])
            elif is_set and t not in ("STR", "OP"):
                break  # ``in (...)`` only counts as a plain tuple of names
    return sorted(found)


def facts_for(path: str, extract: list[str], named: list[str] | None = None) -> dict[str, Any]:
    """What one file contributes (only non-empty parts)."""
    text = read_source(path)
    out: dict[str, Any] = {}
    if path.endswith(".sql"):
        if "ddl" in extract and text.strip():
            out["ddl"] = [norm_sql(text)]
        return out
    wants = set(extract) - {"ddl"}
    if not (wants or named is not None or _DDL.search(text) or "user_version" in text):
        return out  # only DDL was asked for and there is none: no need to read the tokens
    items = lex(text)
    if "ddl" in extract:
        ddl = _ddl(items)
        if ddl:
            out["ddl"] = ddl
    if "consts" in extract:
        consts = _consts(items, None)
        if consts:
            out["consts"] = consts
    if named is not None:
        out["named"] = _consts(items, named)
    if "vlits" in extract:
        lits = _vlits(items)
        if lits:
            out["vlits"] = lits
    if "names" in extract:
        names = _names(items)
        if names:
            out["names"] = names
    if "layout" in extract:
        layout = _layout(items)
        if layout:
            out["layout"] = layout
    return out


def collect(kind: str) -> dict[str, dict[str, Any]]:
    """``{path: facts}`` of every source of a kind that contributes something."""
    plan: dict[str, tuple[set[str], list[str] | None]] = {}  # path -> (what to extract, constants to take by name)
    for rule in RULES[kind]:
        for path, names in rule.get("named", {}).items():
            plan[path] = (plan.get(path, (set(), None))[0], names)
        if "include" in rule:
            for path in _expand(rule["include"], rule["exclude"]):
                extract, names = plan.get(path, (set(), None))
                plan[path] = (extract | set(rule["extract"]), names)
    acc: dict[str, dict[str, Any]] = {}
    for path in sorted(plan):
        facts = facts_for(path, sorted(plan[path][0]), plan[path][1])
        if facts:
            acc[path] = facts
    return acc


# --- steps.py ----------------------------------------------------------------------------------------------


def current_formats() -> dict[str, int]:
    """``CURRENT`` of steps.py, parsed from its text."""
    for names, rhs in _top_assigns(lex(read_source(STEPS))):
        if names == ["CURRENT"]:
            out: dict[str, int] = {}
            for i in range(len(rhs) - 2):
                if rhs[i][0] == "STR" and rhs[i + 1][:2] == ("OP", ":") and rhs[i + 2][0] == "NUM":
                    out[rhs[i][1]] = int(rhs[i + 2][1])
            if out:
                return out
    raise SystemExit(f"cannot find CURRENT in {STEPS}")


def registered_steps() -> dict[tuple[str, int, int], str]:
    """``{(kind, from_n, to_n): path}`` of the steps registered in the migrate package.

    Detected by reading the tokens of ``packages/core/src/orch/migrate/**/*.py`` (``steps.py`` and any step module
    beside it) for a call ``register(Step("<kind>", <from>, <to>, ...))``; the arguments may also be keywords
    (``kind=``, ``from_n=``, ``to_n=``). A step registered any other way (a ``Step`` built first and passed by name,
    a decorator) is not seen, so the guard fails closed: write the call inline."""
    found: dict[tuple[str, int, int], str] = {}
    for path in list_sources(STEPS_GLOB):
        items = lex(read_source(path))
        for i in range(len(items) - 3):
            if [x[:2] for x in items[i : i + 4]] != [
                ("NAME", "register"),
                ("OP", "("),
                ("NAME", "Step"),
                ("OP", "("),
            ]:
                continue
            depth = 1
            j = i + 4
            while j < len(items) and depth:
                if items[j][0] == "OP" and items[j][1] in _OPEN:
                    depth += 1
                elif items[j][0] == "OP" and items[j][1] in _CLOSE:
                    depth -= 1
                j += 1
            args = _split_commas(items[i + 4 : j - 1])
            fields = ("kind", "from_n", "to_n")
            vals: dict[str, Item] = {}
            for pos, arg in enumerate(args[:3]):
                if len(arg) == 1:
                    vals[fields[pos]] = arg[0]
            for arg in args:
                if len(arg) == 3 and arg[0][1] in fields and arg[1][:2] == ("OP", "="):
                    vals[arg[0][1]] = arg[2]
            if all(f in vals for f in fields):
                kind, a, b = (vals[f] for f in fields)
                if kind[0] == "STR" and a[0] == "NUM" and b[0] == "NUM":
                    found[(kind[1], int(a[1]), int(b[1]))] = path
    return found


# --- compute / check / update ------------------------------------------------------------------------------


def compute(kind: str) -> dict[str, Any]:
    """``{"kind", "format", "fingerprint", "sources"}`` of one root kind."""
    if kind not in RULES:
        raise SystemExit(f"unknown kind {kind!r} (one of {', '.join(KINDS)})")
    formats = current_formats()
    if kind not in formats:
        raise SystemExit(f"CURRENT has no entry for {kind!r} in {STEPS}")
    facts = collect(kind)
    canon = json.dumps(
        {"scheme": SCHEME, "kind": kind, "facts": facts}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return {
        "kind": kind,
        "format": formats[kind],
        "fingerprint": hashlib.sha256(canon.encode()).hexdigest(),
        "sources": sorted(facts),
    }


def contract_path(kind: str) -> Path:
    return ROOT / CONTRACT_DIR / f"{kind}.json"


def load_contract(kind: str) -> dict[str, Any] | None:
    try:
        return json.loads(contract_path(kind).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _missing_steps(kind: str, old: int, new: int) -> list[str]:
    have = registered_steps()
    out = []
    for n in range(old, new):
        if (kind, n, n + 1) not in have:
            out.append(
                f"persisted format of {kind} bumped {old}→{new} but no migration step {kind} {n}→{n + 1} "
                f'is registered: add register(Step("{kind}", {n}, {n + 1}, ...)) in '
                "packages/core/src/orch/migrate/ (steps.py or a step module beside it)"
            )
    return out


def check(kind: str) -> list[str]:
    """The problems of one kind (empty: it is fine)."""
    cur = compute(kind)
    old = load_contract(kind)
    if old is None:
        return [f"no contract for {kind}: run scripts/formats.py --update {kind}"]
    was, now = old["format"], cur["format"]
    if now < was:
        return [f"format of {kind} went back from {was} to {now}: a format never goes down"]
    if now == was:
        return [CHANGED.format(kind=kind)] if cur["fingerprint"] != old["fingerprint"] else []
    problems = _missing_steps(kind, was, now)
    if not problems:
        problems.append(
            f"contracts/formats/{kind}.json is stale (format {was}, code {now}): run scripts/formats.py --update {kind}"
        )
    return problems


def update(kind: str, *, allow_same_format: bool = False) -> list[str]:
    """Rewrite the contract of a kind. Refused (the problems are returned) when the fingerprint changed but the format
    did not, or the format was bumped without its step: ``--update`` is how a reviewed change is recorded, never a
    way round the guard. ``allow_same_format`` is for a change of this script's own extraction."""
    cur = compute(kind)
    old = load_contract(kind)
    if old is not None:
        was, now = old["format"], cur["format"]
        if now < was:
            return [f"format of {kind} went back from {was} to {now}: a format never goes down"]
        if now == was and cur["fingerprint"] != old["fingerprint"] and not allow_same_format:
            return [
                f'refusing to update {kind}: the persisted format changed but CURRENT["{kind}"] is still {now}. '
                "Add a migration step and bump it first (--allow-same-format: only for a change of this script)"
            ]
        if now > was:
            missing = _missing_steps(kind, was, now)
            if missing:
                return missing
    path = contract_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="persisted-format guard (see the module docstring)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", nargs="*", metavar="kind", help="compare with contracts/formats (default: all)")
    group.add_argument("--update", nargs="*", metavar="kind", help="rewrite contracts/formats/<kind>.json")
    ap.add_argument("--allow-same-format", action="store_true", help="with --update: scripts/formats.py itself changed")
    args = ap.parse_args(argv)
    chosen = args.check if args.check is not None else args.update
    kinds = list(chosen) or list(KINDS)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        print(f"unknown kind {bad[0]!r} (one of {', '.join(KINDS)})", file=sys.stderr)
        return 2
    failed = False
    for kind in kinds:
        if args.check is not None:
            problems = check(kind)
        else:
            problems = update(kind, allow_same_format=args.allow_same_format)
        for line in problems:
            print(line, file=sys.stderr)
        failed = failed or bool(problems)
        if not problems:
            print(f"{kind}: {'ok' if args.check is not None else 'updated'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
