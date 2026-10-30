#!/usr/bin/env python3
"""Release key ceremony (docs/release-ceremony.md). Run as ``python3 -I scripts/release/ceremony.py <command>``.

    ceremony.py init                  once: root key + CI key rk-1 + first revocation list + embedded root + floor
    ceremony.py resign-revocations    every 14 days: re-sign the revocation list (same serials, newer issued_at)

Every secret stays in this process. The root key lives in Proton Pass (vault Agents) and is read back from there; the
CI key goes to the ``release`` environment secret ORCH_RELEASE_KEY (and into Proton as a backup). Secrets are only
ever passed to another program on STDIN: never in argv, never printed, never written to a file. The only files written
are the public outputs under release/keys/, the embedded root in two source files and the floor in install.sh; they are
staged in a 0700 temp dir that is removed on exit and on every signal. stdout carries only key id, fingerprint,
Proton item IDs (not secret), the changed files and issued_at. Wiping seeds is best effort only: Python keeps
immutable copies of some values; the process is short-lived and never writes them anywhere.
Failure messages are fixed strings: no child output is ever echoed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "packages" / "core" / "src"))
import floor  # noqa: E402
import rootkey  # noqa: E402
from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from manifest import b64, jcs  # noqa: E402

from orch import childenv  # noqa: E402

VAULT = "Agents"
ROOT_TITLE = "orch release root key"
CI_TITLE = "orch release CI key rk-1"
ROOT_ID = "root-1"
CI_ID = "rk-1"
SECRET_NAME = "ORCH_RELEASE_KEY"
GH_REPO = "severinlindenmann/orch-core"
ENV_NAME = "release"
REASON = "orch release key ceremony"
CI_DAYS = 180
ROOT_ENV_LINE = f"ORCH_ROOT_SIGNING_KEY=pass://{VAULT}/{ROOT_TITLE}/password\n"
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: Test seam only: the step name at which to raise (the tests check cleanup after a failure at each step).
_fail_at: str | None = None


class Ceremony(Exception):
    """A refusal or failure. The message is a fixed string and never contains child output or key material."""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stamp(t: datetime) -> str:
    return t.strftime(TIME_FMT)


def _step(name: str) -> None:
    if _fail_at == name:
        raise Ceremony(f"injected failure at {name}")


#: Per tool: the exact environment variables it may inherit on top of ``childenv.minimal`` (HOME, USER, LOGNAME, TMPDIR,
#: LANG, LC_ALL and a fixed PATH). Proxy, CA bundle, debug and log variables are never passed on.
_ALLOW_EXACT = {"pass-cli": (), "gh": ("GH_TOKEN", "GH_CONFIG_DIR")}
_ALLOW_PREFIX = {"pass-cli": ("PROTON_PASS_",), "gh": ()}
_BINS: dict[str, str] = {}


def _resolve_tools() -> None:
    """Resolve both binaries once, to absolute paths, from the PATH this process started with."""
    for name in _ALLOW_EXACT:
        found = shutil.which(name)
        if found is None or not os.path.isabs(found):
            raise Ceremony(f"{name} was not found on PATH")
        _BINS[name] = found


def _child_env(tool: str) -> dict[str, str]:
    env = childenv.minimal(os.environ)
    for k, v in os.environ.items():
        if k in _ALLOW_EXACT[tool] or k.startswith(_ALLOW_PREFIX[tool]):
            env[k] = v
    if tool == "pass-cli":
        env["PROTON_PASS_AGENT_REASON"] = REASON
    return env


def run(argv: list[str], stdin: bytes = b"", timeout: int = 120) -> tuple[int, bytes]:
    """The ONLY way to start a program: argv plus stdin bytes, a per-tool allowlisted env, no shell.

    Returns (exit code, stdout). stderr is captured and dropped, so a child can never leak a value through ours."""
    tool = argv[0]
    if tool not in _BINS:
        raise Ceremony(f"{tool} was not resolved")
    try:
        p = subprocess.run(  # noqa: S603
            [_BINS[tool], *argv[1:]],
            input=stdin,
            capture_output=True,
            env=_child_env(tool),
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, b""
    return p.returncode, p.stdout


def _wipe(*bufs: bytearray) -> None:
    for b in bufs:
        for i in range(len(b)):
            b[i] = 0


def _pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _fingerprint(pub: bytes) -> str:
    return "sha256:" + hashlib.sha256(pub).hexdigest()


def _seed(key: Ed25519PrivateKey) -> bytearray:
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    return bytearray(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


# --- Proton Pass and GitHub, all through run() ----------------------------------------------------------------------


def _walk(node):
    """Yield (title, id-or-None) for every dict in a pass-cli JSON document that has a title (own or in ``content``)."""
    if isinstance(node, dict):
        title = node.get("title")
        if not isinstance(title, str) and isinstance(node.get("content"), dict):
            title = node["content"].get("title")
        if isinstance(title, str):
            item_id = node.get("id") or node.get("item_id")
            yield title, item_id if isinstance(item_id, str) else None
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _pass_items() -> list[tuple[str, str | None]]:
    """Every (title, id) in the vault. An error or unreadable answer REFUSES: absence is never inferred from it."""
    rc, out = run(["pass-cli", "item", "list", "--vault-name", VAULT, "--output", "json"])
    if rc != 0:
        raise Ceremony("pass-cli could not list the vault: refusing (I will not guess that an item is absent)")
    try:
        doc = json.loads(out)
    except ValueError:
        raise Ceremony("pass-cli item list did not return JSON: refusing") from None
    return list(_walk(doc))


def _pass_find_id(title: str) -> str:
    ids = {i for t, i in _pass_items() if t == title and i}
    if len(ids) != 1:
        raise Ceremony(f"expected exactly one vault item titled {title!r}, found {len(ids)}: refusing")
    return next(iter(ids))


def _pass_template() -> dict:
    """The item template of this pass-cli version. If it cannot be read, nothing is stored (no guessed shape)."""
    rc, out = run(["pass-cli", "item", "create", "login", "--get-template"], timeout=10)
    try:
        tpl = json.loads(out) if rc == 0 else None
    except ValueError:
        tpl = None
    if not isinstance(tpl, dict) or not {"title", "username", "password"} <= set(tpl):
        raise Ceremony("cannot read pass-cli's login item template: nothing was stored (confirm the shape first)")
    note_key = "note" if "note" in tpl else "notes" if "notes" in tpl else None
    extra = {k: v for k, v in tpl.items() if k not in {"title", "username", "password", note_key} and v}
    if extra:
        raise Ceremony("pass-cli's login template has unexpected non-empty fields: nothing was stored")
    return tpl


def _created_id(out: bytes) -> str:
    """The item ID that ``create`` printed (JSON ``id``/``item_id``, else the single long token in its output)."""
    text = out.decode(errors="replace")
    try:
        found = {i for _, i in _walk(json.loads(text)) if i}
        if not found:
            doc = json.loads(text)
            found = {doc[k] for k in ("id", "item_id") if isinstance(doc, dict) and isinstance(doc.get(k), str)}
    except ValueError:
        found = set(re.findall(r"[A-Za-z0-9_=-]{16,}", text))
    if len(found) != 1:
        raise Ceremony("the item was created but its ID could not be read from pass-cli's answer: find it in Proton")
    return next(iter(found))


def _pass_store(tpl: dict, title: str, username: str, seed: bytearray, note: str) -> str:
    """Create the login item from a fixed key set (JSON with the base64 seed on stdin). Returns the new item's ID."""
    item = dict(tpl)  # the extra template keys are empty (checked in _pass_template)
    item.update({"title": title, "username": username, "password": base64.b64encode(bytes(seed)).decode()})
    for k in ("note", "notes"):
        if k in tpl:
            item[k] = note
    payload = bytearray(json.dumps(item).encode())
    try:
        rc, out = run(
            ["pass-cli", "item", "create", "login", "--vault-name", VAULT, "--from-template", "-"], bytes(payload)
        )
    finally:
        _wipe(payload)
    if rc != 0:
        raise Ceremony(f"pass-cli could not store the item {title!r}")
    return _created_id(out)


def _pass_read_seed(item_id: str) -> bytearray:
    rc, out = run(["pass-cli", "item", "view", "--vault-name", VAULT, "--item-id", item_id, "--field", "password"])
    if rc != 0:
        raise Ceremony("pass-cli could not read the item (by ID)")
    raw = bytearray(out.strip())
    try:
        seed = bytearray(base64.b64decode(bytes(raw), validate=True))
    except ValueError:
        raise Ceremony("the vault item does not hold a base64 seed") from None
    finally:
        _wipe(raw)
    if len(seed) != 32:
        _wipe(seed)
        raise Ceremony("the vault item does not hold a 32-byte seed")
    return seed


def _store_and_verify(tpl: dict, label: str, title: str, username: str, key: Ed25519PrivateKey, note: str) -> None:
    seed = _seed(key)
    try:
        item_id = _pass_store(tpl, title, username, seed, note)
    finally:
        _wipe(seed)
    print(f"proton item id ({label}): {item_id}", flush=True)  # not secret; recovery names the item by this ID
    back = _pass_read_seed(item_id)
    try:
        same = _pub(Ed25519PrivateKey.from_private_bytes(bytes(back))) == _pub(key)
    finally:
        _wipe(back)
    if not same:
        raise Ceremony(f"read-back of the {label} item does not match the generated key")


def _gh_secret_set(key: Ed25519PrivateKey, repo: str) -> None:
    seed = _seed(key)
    data = bytearray(base64.b64encode(bytes(seed)))
    try:
        rc, _ = run(["gh", "secret", "set", SECRET_NAME, "--env", ENV_NAME, "--repo", repo], bytes(data))
    finally:
        _wipe(seed, data)
    if rc != 0:
        raise Ceremony("gh could not set the release environment secret")


def _gh_json(args: list[str]):
    rc, out = run(["gh", *args])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _secret_present(repo: str) -> bool:
    rc, out = run(["gh", "secret", "list", "--env", ENV_NAME, "--repo", repo])
    if rc != 0:
        return False
    return any(line.split("\t")[0].strip() == SECRET_NAME for line in out.decode(errors="replace").splitlines())


# --- preflight ------------------------------------------------------------------------------------------------------


def _check_clock(headers: str) -> None:
    """Refuse when this machine's clock is more than 5 minutes from GitHub's (it sets every issued_at / not_before)."""
    date = next(
        (v.strip() for k, _, v in (ln.partition(":") for ln in headers.splitlines()) if k.lower() == "date"), ""
    )
    try:
        theirs = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        raise Ceremony("GitHub's answer has no usable Date header: cannot check the clock, refusing") from None
    if theirs.tzinfo is None or abs((_now() - theirs).total_seconds()) > 300:
        raise Ceremony("this machine's clock differs from GitHub's by more than 5 minutes: fix the clock first")


def _check_environment(repo: str) -> None:
    rc, raw = run(["gh", "api", "-i", f"repos/{repo}/environments/{ENV_NAME}"])
    head, sep, body = raw.decode(errors="replace").replace("\r\n", "\n").partition("\n\n")
    try:
        env = json.loads(body) if rc == 0 and sep else None
    except ValueError:
        env = None
    if not isinstance(env, dict):
        raise Ceremony(f"the {ENV_NAME!r} environment does not exist (or gh cannot read it): create it first")
    _check_clock(head)
    if env.get("can_admins_bypass", False) is not False:
        raise Ceremony(f"the {ENV_NAME!r} environment lets admins bypass its protection rules: refusing")
    pol = env.get("deployment_branch_policy")
    if not isinstance(pol, dict) or pol.get("custom_branch_policies") is not True or pol.get("protected_branches"):
        raise Ceremony(f"the {ENV_NAME!r} environment is not restricted to a custom branch policy")
    lst = _gh_json(["api", f"repos/{repo}/environments/{ENV_NAME}/deployment-branch-policies"])
    pols = lst.get("branch_policies") if isinstance(lst, dict) else None
    ok = (
        isinstance(pols, list)
        and len(pols) == 1
        and isinstance(pols[0], dict)
        and pols[0].get("name") == "main"
        and pols[0].get("type", "branch") == "branch"
    )
    if not ok:
        raise Ceremony(f"the {ENV_NAME!r} environment's branch policy is not exactly: main")


def _preflight_init(repo: str, root: Path) -> dict:
    rc, _ = run(["pass-cli", "info"])
    if rc != 0:
        raise Ceremony("pass-cli info failed: log in first (scripts/pass-login.sh)")
    titles = {t for t, _ in _pass_items()}
    for title in (ROOT_TITLE, CI_TITLE):
        if title in titles:
            raise Ceremony(f"the item {title!r} already exists: refusing (rotation is not part of this script)")
    tpl = _pass_template()
    rc, _ = run(["gh", "auth", "status"])
    if rc != 0:
        raise Ceremony("gh is not authenticated")
    _check_environment(repo)
    if (root / "release" / "keys" / "root-1.pub.json").exists():
        raise Ceremony("release/keys/root-1.pub.json already exists: refusing")
    if re.search(r"^PRODUCTION_ROOTS: Mapping\[str, bytes\] = \{\}$", _read(root / KEYS_PY), re.M) is None:
        raise Ceremony("keys.py already embeds production roots: refusing")
    return tpl


# --- file edits -----------------------------------------------------------------------------------------------------

KEYS_PY = "packages/core/src/orch/update/keys.py"
INSTALL_SH = "install.sh"
FLOOR_PY = "packages/core/src/orch/update/floor.py"  # bumped too if a later task adds it (same two lines)
PUBLIC = ("release/keys/root-1.pub.json", "release/keys/rk-1.cert.json", "release/keys/revocations.json")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _json_text(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def embed_root_keys_py(text: str, key_id: str, pub: bytes) -> str:
    old = "PRODUCTION_ROOTS: Mapping[str, bytes] = {}"
    if text.count(old) != 1:
        raise Ceremony("keys.py has an unexpected PRODUCTION_ROOTS line")
    new = f'PRODUCTION_ROOTS: Mapping[str, bytes] = {{\n    "{key_id}": base64.b64decode("{b64(pub)}"),\n}}'
    return text.replace(old, new).replace(
        "# key id -> raw 32-byte Ed25519 public key. Filled by U3b.",
        "# key id -> raw 32-byte Ed25519 public key (set by scripts/release/ceremony.py).",
    )


def embed_root_install_sh(text: str, key_id: str, pub: bytes) -> str:
    old = "ROOTS_B64 = {}"
    if text.count(old) != 1:
        raise Ceremony("install.sh has an unexpected ROOTS_B64 line")
    return text.replace(old, f'ROOTS_B64 = {{"{key_id}": "{b64(pub)}"}}')


def _bump_all(root: Path, staged: dict[str, str], revs_path: Path) -> None:
    issued_at, min_serial = floor.read_list(revs_path)
    for rel in (INSTALL_SH, FLOOR_PY):
        p = root / rel
        if rel in staged:
            text = staged[rel]
        elif p.exists():
            text = _read(p)
        else:
            continue
        staged[rel] = floor.bumped(text, issued_at, min_serial)


def _commit_files(root: Path, staged: dict[str, str]) -> list[str]:
    """Write the staged texts into the tree: release/keys/* first, the embeds last (each via a temp file in its own
    directory, then an atomic rename). On ANY failure the tree is put back from in-memory originals."""
    order = sorted(staged, key=lambda rel: (not rel.startswith("release/keys/"), rel))
    changed: list[str] = []
    originals: dict[str, bytes | None] = {}
    tmps: list[Path] = []
    made_dirs: list[Path] = []
    try:
        for rel in order:
            dest = root / rel
            old = dest.read_bytes() if dest.exists() else None
            if old is not None and old == staged[rel].encode("utf-8"):
                continue
            missing, d = [], dest.parent
            while not d.exists():
                missing.append(d)
                d = d.parent
            dest.parent.mkdir(parents=True, exist_ok=True)
            made_dirs += reversed(missing)
            tmp = dest.with_name(dest.name + ".ceremony-tmp")
            tmps.append(tmp)
            tmp.write_text(staged[rel], encoding="utf-8")
            originals[rel] = old
            os.replace(tmp, dest)
            changed.append(rel)
    except BaseException:
        _rollback(root, originals, tmps, made_dirs)
        raise
    return changed


def _rollback(root: Path, originals: dict[str, bytes | None], tmps: list[Path], made_dirs: list[Path]) -> None:
    """Remove leftover temp files, restore replaced files, delete files and directories this run created."""
    failed = []
    for tmp in tmps:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            failed.append(str(tmp.relative_to(root)))
    for rel, old in originals.items():
        try:
            if old is None:
                (root / rel).unlink(missing_ok=True)
            else:
                (root / rel).write_bytes(old)
        except OSError:
            failed.append(rel)
    for d in reversed(made_dirs):
        try:
            d.rmdir()
        except OSError:
            pass
    if failed:
        raise Ceremony(
            "writing the tree failed AND restoring it failed for: "
            + ", ".join(sorted(failed))
            + " (restore them with git, see docs/release-ceremony.md)"
        )


def _gitignore(root: Path) -> str:
    p = root / ".gitignore"
    text = _read(p) if p.exists() else ""
    if "release/keys/root.env" in text.splitlines():
        return text
    return text + ("" if text.endswith("\n") or not text else "\n") + "release/keys/root.env\n"


# --- commands -------------------------------------------------------------------------------------------------------


def cmd_init(root: Path, repo: str, tmp: Path) -> int:
    _step("preflight")
    tpl = _preflight_init(repo, root)
    now = _now()
    staged: dict[str, str] = {}

    _step("root")
    root_key = Ed25519PrivateKey.generate()
    root_pub = _pub(root_key)
    fp = _fingerprint(root_pub)
    note = f"orch release root key {ROOT_ID}; created {_stamp(now)}; fingerprint {fp}. Never leaves Proton Pass."
    _store_and_verify(tpl, "root", ROOT_TITLE, ROOT_ID, root_key, note)

    _step("ci-key")
    ci_key = Ed25519PrivateKey.generate()
    cert_doc = rootkey.build_cert(
        key_id=CI_ID,
        public_key=_pub(ci_key),
        serial=1,
        purpose="core",
        channels=["main"],
        not_before=_stamp(now),
        not_after=_stamp(now + timedelta(days=CI_DAYS)),
    )
    cert = rootkey.certify(root_key, ROOT_ID, cert_doc)
    ci_note = (
        f"orch release CI key {CI_ID}; created {_stamp(now)}; core/main only; restores the GitHub secret {SECRET_NAME}."
    )
    _step("ci-proton")
    _store_and_verify(tpl, "ci", CI_TITLE, CI_ID, ci_key, ci_note)
    _step("ci-secret")
    _gh_secret_set(ci_key, repo)

    _step("revocations")
    revs = rootkey.revoke(root_key, ROOT_ID, rootkey.build_revocations(min_serial=1, revoked=[], issued_at=_stamp(now)))

    _step("stage")
    staged["release/keys/root-1.pub.json"] = _json_text(
        {"fingerprint": fp, "key_id": ROOT_ID, "public_key": b64(root_pub)}
    )
    staged["release/keys/rk-1.cert.json"] = _json_text(cert)
    staged["release/keys/revocations.json"] = _json_text(revs)
    staged["release/keys/root.env"] = ROOT_ENV_LINE
    staged[".gitignore"] = _gitignore(root)
    staged[KEYS_PY] = embed_root_keys_py(_read(root / KEYS_PY), ROOT_ID, root_pub)
    staged[INSTALL_SH] = embed_root_install_sh(_read(root / INSTALL_SH), ROOT_ID, root_pub)
    # the public files go through the temp dir first, so the floor is computed from the very file that gets written
    stage_dir = tmp / "stage"
    stage_dir.mkdir(mode=0o700)
    (stage_dir / "revocations.json").write_text(staged["release/keys/revocations.json"], encoding="utf-8")
    _bump_all(root, staged, stage_dir / "revocations.json")

    _step("write")
    changed = _commit_files(root, staged)
    del root_key, ci_key

    print(f"root key id: {ROOT_ID}")
    print(f"fingerprint: {fp}")
    print(f"issued_at: {_stamp(now)}")
    print("changed files:")
    for rel in changed:
        print(f"  {rel}")
    _step("secret-list")
    print(f"{SECRET_NAME} in environment {ENV_NAME}: {'present' if _secret_present(repo) else 'MISSING'}")
    return 0


def _embedded_root(root: Path) -> bytes:
    """The root public key embedded in keys.py, which must equal the one embedded in install.sh."""
    k = re.search(
        r'PRODUCTION_ROOTS: Mapping\[str, bytes\] = \{\s*"root-1": base64\.b64decode\("([^"]+)"\),\s*\}',
        _read(root / KEYS_PY),
    )
    i = re.search(r'^ROOTS_B64 = \{"root-1": "([^"]+)"\}$', _read(root / INSTALL_SH), re.M)
    if k is None or i is None or k[1] != i[1]:
        raise Ceremony("no single embedded root-1 in keys.py and install.sh: refusing")
    try:
        pub = base64.b64decode(k[1], validate=True)
    except ValueError:
        raise Ceremony("the embedded root is not base64") from None
    if len(pub) != 32:
        raise Ceremony("the embedded root is not 32 bytes")
    return pub


def _verified_current(root: Path, revs_path: Path, embedded: bytes) -> dict:
    """The current list, accepted only as a signed envelope that verifies against ``embedded`` and is >= the floor."""
    try:
        env = json.loads(_read(revs_path))
        if not isinstance(env, dict) or set(env) != {"doc", "root_key_id", "sig"} or env["root_key_id"] != ROOT_ID:
            raise ValueError
        body = base64.b64decode(env["doc"], validate=True)
        sig = base64.b64decode(env["sig"], validate=True)
        Ed25519PublicKey.from_public_bytes(embedded).verify(sig, rootkey.REVOCATIONS_DOMAIN + body)
        doc = json.loads(body)
        if jcs(doc) != body:
            raise ValueError
        prev = rootkey.build_revocations(
            min_serial=doc["min_serial"], revoked=doc["revoked"], issued_at=doc["issued_at"]
        )
        if set(doc) != set(prev) or doc["format"] != prev["format"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, InvalidSignature):
        raise Ceremony("the current revocations.json is not a validly signed list (embedded root): refusing") from None
    for rel in (INSTALL_SH, FLOOR_PY):
        p = root / rel
        if p.exists():
            f_at, f_serial = floor.read_floor(_read(p))
            if prev["issued_at"] < f_at or prev["min_serial"] < f_serial:
                raise Ceremony(f"the current list is below the floor in {rel}: refusing")
    return prev


def cmd_resign(root: Path, repo: str, tmp: Path) -> int:
    _step("preflight")
    rc, _ = run(["pass-cli", "info"])
    if rc != 0:
        raise Ceremony("pass-cli info failed: log in first (scripts/pass-login.sh)")
    revs_path = root / "release" / "keys" / "revocations.json"
    if not revs_path.exists():
        raise Ceremony("release/keys/revocations.json is missing: run init first")
    # The current list is only trusted after its signature verifies against the EMBEDDED root (never a key file in
    # the tree) and it is not below the floor. All of this happens before the root key is fetched.
    _step("verify-current")
    embedded = _embedded_root(root)
    prev = _verified_current(root, revs_path, embedded)
    print(f"re-signing: min_serial {prev['min_serial']}, revoked {prev['revoked']}")

    _step("read-root")
    seed = _pass_read_seed(_pass_find_id(ROOT_TITLE))
    try:
        key = Ed25519PrivateKey.from_private_bytes(bytes(seed))
    finally:
        _wipe(seed)
    if _pub(key) != embedded:
        raise Ceremony("the root key in Proton Pass is not the embedded root: refusing to sign")

    _step("sign")
    issued_at = _stamp(_now())
    if issued_at <= prev["issued_at"]:
        raise Ceremony("the clock is not after the current list's issued_at: refusing")
    new = rootkey.build_revocations(min_serial=prev["min_serial"], revoked=prev["revoked"], issued_at=issued_at)
    try:
        rootkey.check_successor(new, prev)
    except ValueError:
        raise Ceremony("the new list is not a valid successor of the current one") from None
    out = rootkey.revoke(key, ROOT_ID, new)
    del key

    _step("stage")
    staged = {"release/keys/revocations.json": _json_text(out)}
    stage_dir = tmp / "stage"
    stage_dir.mkdir(mode=0o700)
    (stage_dir / "revocations.json").write_text(staged["release/keys/revocations.json"], encoding="utf-8")
    _bump_all(root, staged, stage_dir / "revocations.json")

    _step("write")
    changed = _commit_files(root, staged)
    print(f"issued_at: {issued_at}")
    print("changed files:")
    for rel in changed:
        print(f"  {rel}")
    return 0


# --- entry ----------------------------------------------------------------------------------------------------------


def _on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("init", "resign-revocations"))
    ap.add_argument("--repo-root", type=Path, default=HERE.parents[1], help=argparse.SUPPRESS)
    ap.add_argument("--gh-repo", default=GH_REPO, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    tmp = Path(tempfile.mkdtemp(prefix="orch-ceremony-"))
    try:
        _resolve_tools()
        os.chmod(tmp, 0o700)
        fn = cmd_init if a.command == "init" else cmd_resign
        return fn(a.repo_root, a.gh_repo, tmp)
    except Ceremony as exc:
        print(f"ceremony: {exc}", file=sys.stderr)
        print("ceremony: stopped; see docs/release-ceremony.md (recovery)", file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError):
        print("ceremony: stopped on an unexpected error; see docs/release-ceremony.md (recovery)", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
