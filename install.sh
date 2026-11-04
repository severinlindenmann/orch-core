#!/bin/sh
# orch installer for macOS and Ubuntu (Linux).   Usage:  sh install.sh [--channel main] [--server] [--from DIR|URL]
#
#   --channel C   release channel (default: main)
#   --server      Ubuntu server: check that your user may run services without a login, then say what to do next
#   --from X      take the release from a folder or URL instead of GitHub (tests and offline use)
#
# What it does, in this order, and nothing else: install a pinned uv (checked against a built-in sha256), download the
# signed release information, VERIFY it (signature chain, expiry, channel, then every file's size and sha256) with a
# small stdlib-only checker built into this file, and ONLY THEN install the verified wheels, hash-locked, into
# <state>/install/tools/orch-<version>/ and flip <state>/install/current to it. Any failure stops with one sentence and
# leaves orch as it was. Trust on the very first install is the TLS connection that delivered this file.
#
# LAYOUT: see orch/update/layout.py  (state = ORCH_STATE_DIR, else $XDG_CONFIG_HOME/orch, else ~/.config/orch)
#   <state>/install/tools/orch-<ver>/venv/    one environment per version (A/B); .complete marks a finished one
#   <state>/install/current                   symlink to tools/orch-<ver>; flipped atomically (rename over the link)
#   ~/.local/bin/orch                         symlink to <state>/install/current/venv/bin/orch (never changes)
#   <state>/format.json                       host data format, written only when absent
#   <data>/install.json                       install record (data = $XDG_DATA_HOME/orch, else ~/.local/share/orch)
#   <data>/uv/uv-<pinned version>/uv          the pinned uv, when none of that version was found
#
# Test knob: ORCH_INSTALL_UV_BASE (a folder, or an https URL) overrides where the uv archive is fetched from; its sha256 is still checked.
#
# Everything runs inside main, called on the last line: a download cut short runs nothing.

set -eu

UV_VERSION="0.13.3"
UV_SHA256_aarch64_apple_darwin="bf1e4fb6f5f5079a2902ad3f3a9e2d337575b226a1378f2449d8404353650608"
UV_SHA256_x86_64_apple_darwin="5c35634957db7ebd9c2e4cc6c66b8979fb2c26a6b0dbd1d214d6acbac4033a89"
UV_SHA256_x86_64_unknown_linux_gnu="e4f169ec380f1793b898a6f1fcbacd22db25cf7ca0349a6e96ffb85b69998d04"
UV_SHA256_aarch64_unknown_linux_gnu="611ff684f193a84d2fdad29c5f12505d02ece94984c7c982cdde950eba5c95b9"
# sha256 of the extracted uv binary itself: the only thing that lets an already-installed uv be reused
UV_BIN_SHA256_aarch64_apple_darwin="9e1c4b0585ee3042fa8624229fb2897231edf8194fe64577be31087b210e367d"
UV_BIN_SHA256_x86_64_apple_darwin="5c0532bffe173c7f21d45b8e89fc6604e5143348ee447e5d6df4ce75bd7013e1"
UV_BIN_SHA256_x86_64_unknown_linux_gnu="90c2ff84aa2a76a1ae70454ede3c6a9a4a40c786ccbf777148341a05cb583018"
UV_BIN_SHA256_aarch64_unknown_linux_gnu="0bbbc387a140ee29a80ef14a556368f8bcdacfb4320a4aca9ce48eb239760511"
PYTHON_VERSION="3.14"
RELEASE_REPO="severinlindenmann/orch-core"
HOST_FORMAT="1" # the host data format this release writes (orch.migrate.steps.CURRENT["host"]); a test pins it
TAB=$(printf '\t')

T=""        # private temp dir of this run
VDIR=""     # version directory of this run
BUILT=""    # the version directory THIS run is building (removed again if the run fails before the flip)

say() { printf '%s\n' "$*"; }
note() { printf '%s\n' "$*" >&2; }
die() { note "$*"; exit 1; }

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ -n "$BUILT" ] && [ -d "$BUILT" ]; then rm -rf "$BUILT"; fi
  if [ -n "$T" ] && [ -d "$T" ]; then rm -rf "$T"; fi
  exit "$status"
}

usage() {
  cat <<'EOF'
Usage: sh install.sh [--channel main] [--server] [--from DIR|URL]
  --channel C   release channel (default: main)
  --server      Ubuntu server: check that your user may run services without a login, then say what to do next
  --from X      take the release from a folder or URL instead of GitHub (tests and offline use)
EOF
}

explain() {
  case "$1" in
    malformed) echo "the release information is damaged" ;;
    unknown_root | bad_cert_sig | bad_revocations_sig | wrong_purpose | channel_not_allowed | cert_not_yet_valid \
      | cert_expired | key_revoked | key_mismatch | bad_sig | wrong_format) echo "its signature or signing key is not valid" ;;
    wrong_channel) echo "it belongs to a different channel" ;;
    expired) echo "it has expired" ;;
    rollback | revocations_rollback) echo "it is older than what this computer has already seen" ;;
    revocations_from_future) echo "its revocation list is dated in the future; check this computer's clock" ;;
    revocations_below_floor) echo "its revocation list is older than this installer accepts: get a newer install.sh" ;;
    revocations_stale) echo "this release's revocation list is too old; try again later or tell the person who runs orch releases" ;;
    size_mismatch | hash_mismatch) echo "a downloaded file does not match what was signed" ;;
    *) echo "it could not be checked" ;;
  esac
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d ' ' -f 1
  else sha256sum "$1" | cut -d ' ' -f 1; fi
}

write_verifier() {
cat > "$T/verify.py" <<'PYEOF'
"""orch install.sh verifier. Standard library only, run as `python -I verify.py ...` (CONTRACT.md in
packages/core/tests/vectors/release is the spec; orch/update/verify.py is the same checker in core)."""
import base64
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

# ROOTS-BEGIN  the release root public keys: key id -> base64 of the raw 32-byte Ed25519 key (none until the ceremony)
ROOTS_B64 = {"root-1": "AqlVu40nbMo5blJaZ9oEfSHp3mx6jBlHwUjcUDGabLE="}
# ROOTS-END
ALLOW_TEST_CHANNELS = False  # a production installer refuses every test-* channel
# FLOOR-BEGIN  the oldest revocation list a FIRST install accepts (scripts/release/floor.py rewrites both lines per release)
REVOCATIONS_FLOOR_ISSUED_AT = "2026-10-30T23:25:41Z"
REVOCATIONS_FLOOR_MIN_SERIAL = 1
# FLOOR-END
REVOCATIONS_MAX_AGE_DAYS = 30  # and the list must be fresh: the scheduled re-sign refreshes it with every manifest

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def _decode(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _same(p, q):
    return (p[0] * q[2] - q[0] * p[2]) % P == 0 and (p[1] * q[2] - q[1] * p[2]) % P == 0


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def ed25519_verify(pub, sig, msg):
    if len(pub) != 32 or len(sig) != 64:
        return False
    a_pt, r_pt = _decode(pub), _decode(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if a_pt is None or r_pt is None or s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % L
    return _same(_mul(s, G), _add(r_pt, _mul(h, a_pt)))


class Malformed(Exception):
    pass


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


MAX_INT = 2**53 - 1
A = re.ASCII
TIME_RX = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z", A)
CHANNEL_RX = re.compile(r"(main|develop|test-[a-z0-9][a-z0-9-]{0,30})\Z", A)
KEY_ID_RX = re.compile(r"[a-z][a-z0-9-]{0,31}\Z", A)
VERSION_RX = re.compile(r"\d+\.\d+\.\d+(\.dev\d+)?\Z", A)
ROLES = ("core", "dashboard", "hub", "relay", "publish", "constraints")
NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", A)
HEX64_RX = re.compile(r"[0-9a-f]{64}\Z", A)
HEX40_RX = re.compile(r"[0-9a-f]{40}\Z", A)
CERT_DOMAIN = b"orch-release-key/1\n"
REVOCATIONS_DOMAIN = b"orch-revocations/1\n"
PAYLOAD_DOMAIN = b"orch-release/1\n"


def need(cond):
    if not cond:
        raise Malformed


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        need(k not in out)
        out[k] = v
    return out


def _no(_):
    raise Malformed


def strict_json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_no, parse_constant=_no)
    except (ValueError, RecursionError):
        raise Malformed from None


def in_domain(v, depth=0):
    if isinstance(v, (bool, str)):
        return True
    if isinstance(v, int):
        return 0 <= v <= MAX_INT
    if isinstance(v, list):
        return depth < 32 and all(in_domain(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return depth < 32 and all(k.isascii() and in_domain(x, depth + 1) for k, x in v.items())
    return False


def parse_canonical(data):
    obj = strict_json(data)
    need(isinstance(obj, dict) and in_domain(obj))
    try:
        again = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (UnicodeError, RecursionError):
        raise Malformed from None
    need(again == data)
    return obj


def keys_are(obj, want):
    need(isinstance(obj, dict) and set(obj) == want)
    return obj


def text(v):
    need(isinstance(v, str))
    return v


def b64(v, size=None):
    s = text(v)
    try:
        raw = base64.b64decode(s, validate=True)
    except ValueError:
        raise Malformed from None
    need(base64.b64encode(raw).decode("ascii") == s and (size is None or len(raw) == size))
    return raw


def parse_time(v):
    s = text(v)
    need(bool(TIME_RX.match(s)))
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise Malformed from None


def uint(v, lo=1):
    need(type(v) is int and lo <= v <= MAX_INT)
    return v


def matches(v, rx):
    s = text(v)
    need(bool(rx.match(s)))
    return s


def parse_envelope(data):
    env = keys_are(strict_json(data), {"format", "payload", "sig", "key_cert", "revocations"})
    need(env["format"] == "orch.release-envelope/1")
    sig = keys_are(env["sig"], {"alg", "key_id", "value"})
    need(sig["alg"] == "ed25519")
    kc = keys_are(env["key_cert"], {"cert", "root_key_id", "sig"})
    rv = keys_are(env["revocations"], {"doc", "root_key_id", "sig"})
    return {
        "payload": b64(env["payload"]),
        "sig_key_id": matches(sig["key_id"], KEY_ID_RX),
        "sig_value": b64(sig["value"], 64),
        "cert_bytes": b64(kc["cert"]),
        "cert_root": matches(kc["root_key_id"], KEY_ID_RX),
        "cert_sig": b64(kc["sig"], 64),
        "revs_bytes": b64(rv["doc"]),
        "revs_root": matches(rv["root_key_id"], KEY_ID_RX),
        "revs_sig": b64(rv["sig"], 64),
    }


def check_cert(c):
    keys_are(c, {"format", "key_id", "public_key", "serial", "purpose", "channels", "not_before", "not_after"})
    need(c["format"] == "orch.release-key/1")
    matches(c["key_id"], KEY_ID_RX)
    pub = b64(c["public_key"], 32)
    uint(c["serial"])
    need(c["purpose"] in ("core", "addon-catalog"))
    ch = c["channels"]
    need(isinstance(ch, list) and bool(ch) and len(set(ch)) == len(ch))
    need(all(isinstance(x, str) and (x == "test-*" or CHANNEL_RX.match(x)) for x in ch))
    need(parse_time(c["not_before"]) < parse_time(c["not_after"]))
    return pub


def check_revocations(d):
    keys_are(d, {"format", "min_serial", "revoked", "issued_at"})
    need(d["format"] == "orch.revocations/1")
    uint(d["min_serial"])
    r = d["revoked"]
    need(isinstance(r, list) and all(type(x) is int and 1 <= x <= MAX_INT for x in r) and r == sorted(set(r)))
    parse_time(d["issued_at"])


def check_manifest(m):
    keys_are(
        m,
        {"format", "channel", "version", "seq", "commit", "issued_at", "expires_at", "files", "changelog", "min_from"},
    )
    matches(m["channel"], CHANNEL_RX)
    matches(m["version"], VERSION_RX)
    matches(m["min_from"], VERSION_RX)
    uint(m["seq"])
    matches(m["commit"], HEX40_RX)
    need(parse_time(m["issued_at"]) < parse_time(m["expires_at"]))
    need(len(text(m["changelog"]).encode("utf-8")) <= 4096)
    fs = m["files"]
    need(isinstance(fs, list) and 1 <= len(fs) <= len(ROLES))
    for f in fs:
        keys_are(f, {"role", "name", "sha256", "size", "url"})
        matches(f["name"], NAME_RX)
        matches(f["sha256"], HEX64_RX)
        uint(f["size"])
        url = text(f["url"])
        need(url.startswith("https://") and len(url) <= 512 and url.isprintable() and " " not in url)
    roles = [f["role"] for f in fs]
    need(all(isinstance(r, str) and r in ROLES for r in roles))
    need(len(set(roles)) == len(roles))
    need("core" in roles and "constraints" in roles)
    need(len({f["name"].lower() for f in fs}) == len(fs))


def channel_allowed(allowed, channel):
    return channel in allowed or (channel.startswith("test-") and "test-*" in allowed)


def verify(envelope, channel, now, seq_seen, revocations_issued_at, files=None, roots=None):
    """CONTRACT.md section 3, purpose "core". `files` maps a name to bytes (anything with .get). Raises Refused."""
    trusted = {k: base64.b64decode(v) for k, v in ROOTS_B64.items()} if roots is None else roots
    try:
        return _verify(envelope, channel, now, seq_seen, revocations_issued_at, files, trusted)
    except (Malformed, RecursionError):
        raise Refused("malformed") from None


def _verify(envelope, channel, now, seq_seen, stored_revs, files, trusted):
    env = parse_envelope(envelope)
    root = trusted.get(env["cert_root"])
    if root is None:
        raise Refused("unknown_root")
    if not ed25519_verify(root, env["cert_sig"], CERT_DOMAIN + env["cert_bytes"]):
        raise Refused("bad_cert_sig")
    cert = parse_canonical(env["cert_bytes"])
    cert_pub = check_cert(cert)

    rroot = trusted.get(env["revs_root"])
    if rroot is None:
        raise Refused("unknown_root")
    if not ed25519_verify(rroot, env["revs_sig"], REVOCATIONS_DOMAIN + env["revs_bytes"]):
        raise Refused("bad_revocations_sig")
    revs = parse_canonical(env["revs_bytes"])
    check_revocations(revs)
    if stored_revs is not None and parse_time(revs["issued_at"]) < parse_time(stored_revs):
        raise Refused("revocations_rollback")

    if cert["purpose"] != "core":
        raise Refused("wrong_purpose")
    if not channel_allowed(cert["channels"], channel) or (channel.startswith("test-") and not ALLOW_TEST_CHANNELS):
        raise Refused("channel_not_allowed")
    if now < parse_time(cert["not_before"]):
        raise Refused("cert_not_yet_valid")
    if now >= parse_time(cert["not_after"]):
        raise Refused("cert_expired")
    if cert["serial"] < revs["min_serial"] or cert["serial"] in revs["revoked"]:
        raise Refused("key_revoked")

    if env["sig_key_id"] != cert["key_id"]:
        raise Refused("key_mismatch")
    if not ed25519_verify(cert_pub, env["sig_value"], PAYLOAD_DOMAIN + env["payload"]):
        raise Refused("bad_sig")

    doc = parse_canonical(env["payload"])
    if doc.get("format") != "orch.release/1":
        raise Refused("wrong_format")
    check_manifest(doc)
    if doc["channel"] != channel:
        raise Refused("wrong_channel")
    if now >= parse_time(doc["expires_at"]):
        raise Refused("expired")
    if seq_seen is not None and doc["seq"] < seq_seen:
        raise Refused("rollback")

    if files is not None:
        for f in doc["files"]:
            data = files.get(f["name"])
            if data is None or len(data) != f["size"]:
                raise Refused("size_mismatch")
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise Refused("hash_mismatch")

    return {
        "channel": doc["channel"],
        "seq": doc["seq"],
        "version": doc["version"],
        "min_serial": revs["min_serial"],
        "revocations_issued_at": revs["issued_at"],
        "document": doc,
        "root_key_id": env["cert_root"],
        "release_key_id": cert["key_id"],
    }


# ---- command line (everything below is used by install.sh only) -------------------------------------------------


class DirFiles:
    """Files of a download directory, by the verified manifest name only (a symlink or a directory is not a file)."""

    def __init__(self, path):
        self.path = path

    def get(self, name):
        p = os.path.join(self.path, name)
        if os.path.islink(p) or not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()


def fail(msg, code=2):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def read_record(path):
    """The install record, or None when there is none. A record that cannot be read stops the install."""
    try:
        with open(path, "rb") as f:
            raw = f.read(1 << 20)
    except FileNotFoundError:
        return None
    except OSError:
        fail("RECORD the install record cannot be read")
    try:
        rec = json.loads(raw)
        if not isinstance(rec, dict) or not isinstance(rec.get("seq_seen"), dict):
            raise ValueError
        if rec.get("revocations_issued_at") is not None:
            parse_time(rec["revocations_issued_at"])
        for k, v in rec["seq_seen"].items():
            if type(v) is not int or v < 1:
                raise ValueError
    except (ValueError, Malformed):
        fail("RECORD the install record is damaged")
    return rec


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def cmd_check():
    channel = arg("--channel")
    if not channel or not CHANNEL_RX.match(channel):
        fail("bad channel")
    if not ROOTS_B64:
        fail("NOKEY", 3)
    rec = read_record(arg("--record")) if arg("--record") else None
    seq_seen = revs_at = None
    if rec is not None:
        seq_seen = rec["seq_seen"].get(channel)
        revs_at = rec.get("revocations_issued_at")
    with open(arg("--envelope"), "rb") as f:
        env = f.read(1 << 20)
    files = DirFiles(arg("--files")) if arg("--files") else None
    now = datetime.now(timezone.utc)
    try:
        v = verify(env, channel, now, seq_seen, revs_at, files)
    except Refused as r:
        fail("REFUSED " + r.reason, 1)
    if v["min_serial"] < REVOCATIONS_FLOOR_MIN_SERIAL or parse_time(v["revocations_issued_at"]) < parse_time(
        REVOCATIONS_FLOOR_ISSUED_AT
    ):
        fail("REFUSED revocations_below_floor", 1)
    if (parse_time(v["revocations_issued_at"]) - now).total_seconds() > 86400:
        fail("REFUSED revocations_from_future", 1)
    if (now - parse_time(v["revocations_issued_at"])).days >= REVOCATIONS_MAX_AGE_DAYS:
        fail("REFUSED revocations_stale", 1)
    doc = v["document"]
    if arg("--json-out"):
        out = {k: v[k] for k in ("channel", "seq", "version", "min_serial", "revocations_issued_at", "root_key_id",
                                  "release_key_id")}
        out["files"] = [{k: f[k] for k in ("role", "name", "sha256", "size")} for f in doc["files"]]
        out["trusted_roots"] = sorted(ROOTS_B64)
        write_private(arg("--json-out"), json.dumps(out, sort_keys=True).encode())
    print("VERSION\t" + doc["version"])
    print("SEQ\t%d" % doc["seq"])
    for f in doc["files"]:
        print("FILE\t%s\t%s\t%d\t%s" % (f["role"], f["name"], f["size"], f["url"]))


def write_private(path, data):
    """Write `data` to `path` atomically, mode 0600 (tmp file in the same directory, fsync, rename)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def cmd_reqs():
    """Requirements for `uv pip install --require-hashes`: the signed constraints plus every wheel with its hash."""
    d = arg("--files")
    with open(arg("--check-json"), "rb") as f:
        info = json.load(f)
    lines = []
    for f in info["files"]:
        if f["role"] == "constraints":
            with open(os.path.join(d, f["name"]), "r", encoding="utf-8") as c:
                lines.append(c.read().rstrip("\n"))
    for f in info["files"]:
        if f["role"] != "constraints":
            uri = pathlib.Path(os.path.abspath(os.path.join(d, f["name"]))).as_uri()
            lines.append("%s --hash=sha256:%s" % (uri, f["sha256"]))
    sys.stdout.write("\n".join(lines) + "\n")


def cmd_record():
    path = arg("--record")
    with open(arg("--check-json"), "rb") as f:
        info = json.load(f)
    old = read_record(path)
    seq = dict(old["seq_seen"]) if old else {}
    seq[info["channel"]] = max(seq.get(info["channel"], 0), info["seq"])
    rec = {
        "format": "orch.install/1",
        "channel": info["channel"],
        "version": info["version"],
        "seq_seen": seq,
        "revocations_issued_at": info["revocations_issued_at"],
        "min_serial": info["min_serial"],
        "root_key_id": info["root_key_id"],
        "release_key_id": info["release_key_id"],
        "trusted_roots": info["trusted_roots"],
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_dir": arg("--tool-dir"),
        "uv": {"path": arg("--uv"), "version": arg("--uv-version")},
        "files": info["files"],
    }
    write_private(path, json.dumps(rec, sort_keys=True, indent=1).encode() + b"\n")


def cmd_flip():
    """Point the symlink --link at --target atomically (rename over the old link)."""
    link, target = arg("--link"), arg("--target")
    tmp = "%s.new-%d" % (link, os.getpid())
    os.makedirs(os.path.dirname(link), mode=0o700, exist_ok=True)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, link)


def cmd_hostformat():
    """Write <state>/format.json (the host data format) unless it exists. Same bytes as orch.migrate write_format."""
    state, n = arg("--state"), int(arg("--format"))
    os.makedirs(state, mode=0o700, exist_ok=True)
    final = os.path.join(state, "format.json")
    if os.path.lexists(final):
        return
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=state)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps({"orch": "host", "format": n}, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, final)  # fails when a format.json appeared meanwhile: never overwrite
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    table = {"check": cmd_check, "reqs": cmd_reqs, "record": cmd_record, "flip": cmd_flip, "hostformat": cmd_hostformat}
    if cmd not in table:
        fail("unknown command")
    table[cmd]()


if __name__ == "__main__":
    main()
PYEOF
}

find_platform() {
  OS=$(uname -s)
  ARCH=$(uname -m)
  case "$OS/$ARCH" in
    Darwin/arm64) TRIPLE="aarch64-apple-darwin" ;;
    Darwin/x86_64) TRIPLE="x86_64-apple-darwin" ;;
    Linux/x86_64) TRIPLE="x86_64-unknown-linux-gnu" ;;
    Linux/aarch64 | Linux/arm64) TRIPLE="aarch64-unknown-linux-gnu" ;;
    *) die "This computer ($OS $ARCH) is not supported. orch runs on macOS and on Linux (x86_64, aarch64)." ;;
  esac
  if [ "$OS" = Darwin ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
    die "This terminal runs under Rosetta. Open a native terminal and run the installer again."
  fi
  if [ "$OS" = Linux ] && ldd --version 2>&1 | grep -qi musl; then
    die "This Linux uses musl (for example Alpine). orch needs a glibc Linux such as Ubuntu."
  fi
}

check_linger() {
  [ "$OS" = Linux ] || die "--server is for an Ubuntu (Linux) server. On a Mac, run the installer without it."
  command -v loginctl >/dev/null 2>&1 || die "This server has no loginctl, so it cannot keep services running after you log out. Use Ubuntu with systemd."
  who=$(id -un)
  linger=$(loginctl show-user "$who" -p Linger 2>/dev/null | sed -n 's/^Linger=//p')
  if [ "$linger" != yes ]; then
    note "Your user ($who) cannot keep services running after you log out yet."
    note "Run this one line (it needs administrator rights, and the installer never runs sudo for you), then run the installer again:"
    note ""
    note "    sudo loginctl enable-linger $who"
    exit 1
  fi
}

fetch() { # fetch URL OUT
  curl -fsSL --retry 2 --connect-timeout 20 --max-time 900 --proto '=https' --tlsv1.2 "$1" -o "$2" </dev/null
}

get_uv() {
  case "$TRIPLE" in
    aarch64-apple-darwin) want=$UV_SHA256_aarch64_apple_darwin; want_bin=$UV_BIN_SHA256_aarch64_apple_darwin ;;
    x86_64-apple-darwin) want=$UV_SHA256_x86_64_apple_darwin; want_bin=$UV_BIN_SHA256_x86_64_apple_darwin ;;
    x86_64-unknown-linux-gnu) want=$UV_SHA256_x86_64_unknown_linux_gnu; want_bin=$UV_BIN_SHA256_x86_64_unknown_linux_gnu ;;
    aarch64-unknown-linux-gnu) want=$UV_SHA256_aarch64_unknown_linux_gnu; want_bin=$UV_BIN_SHA256_aarch64_unknown_linux_gnu ;;
    *) die "No pinned uv for $TRIPLE. Nothing was installed." ;;
  esac
  # Reuse only OUR copy, and only when its bytes are exactly the pinned binary. A uv found on PATH is never trusted.
  UV="$DATA/uv/uv-$UV_VERSION/uv"
  if [ -f "$UV" ] && [ ! -L "$UV" ] && [ -x "$UV" ] && [ "$(sha256_of "$UV")" = "$want_bin" ]; then return 0; fi
  UV=""
  base="${ORCH_INSTALL_UV_BASE:-https://github.com/astral-sh/uv/releases/download/$UV_VERSION}"
  say "Fetching uv $UV_VERSION (the tool that installs orch) ..."
  if [ -d "$base" ]; then cp "$base/uv-$TRIPLE.tar.gz" "$T/uv.tar.gz" 2>/dev/null || die "Could not read uv from $base. Nothing was installed."
  else fetch "$base/uv-$TRIPLE.tar.gz" "$T/uv.tar.gz" || die "Could not download uv. Nothing was installed."; fi
  got=$(sha256_of "$T/uv.tar.gz")
  if [ "$got" != "$want" ]; then
    die "The uv download does not match the checksum built into this installer, so it was not used. Nothing was installed."
  fi
  mkdir -p "$T/uvx"
  tar -xzf "$T/uv.tar.gz" -C "$T/uvx" --strip-components=1 "uv-$TRIPLE/uv" || die "Could not unpack uv. Nothing was installed."
  mkdir -p "$DATA/uv/uv-$UV_VERSION"
  cp "$T/uvx/uv" "$DATA/uv/uv-$UV_VERSION/uv.new"
  chmod 755 "$DATA/uv/uv-$UV_VERSION/uv.new"
  mv -f "$DATA/uv/uv-$UV_VERSION/uv.new" "$DATA/uv/uv-$UV_VERSION/uv"
  UV="$DATA/uv/uv-$UV_VERSION/uv"
  [ "$("$UV" --version | cut -d ' ' -f 2)" = "$UV_VERSION" ] || die "The unpacked uv is not version $UV_VERSION. Nothing was installed."
}

get_python() {
  PY=$("$UV" python find --no-config "$PYTHON_VERSION" 2>/dev/null || true)
  if [ -z "$PY" ]; then
    say "Fetching Python $PYTHON_VERSION ..."
    "$UV" python install --no-config "$PYTHON_VERSION" >&2 || die "Could not get Python $PYTHON_VERSION. Nothing was installed."
    PY=$("$UV" python find --no-config "$PYTHON_VERSION" 2>/dev/null || true)
  fi
  [ -n "$PY" ] || die "Python $PYTHON_VERSION is not available. Nothing was installed."
}

stop_unverified() { # stop_unverified <stderr file of the verifier>
  line=$(sed -n '1p' "$1")
  case "$line" in
    "REFUSED "*)
      reason=${line#REFUSED }
      die "Stopped: the download could not be verified ($(explain "$reason")). Nothing was installed or changed."
      ;;
    NOKEY*) die "Stopped: this copy of the installer has no release key built in, so it cannot verify anything. Get install.sh from the official release. Nothing was installed." ;;
    "RECORD "*) die "Stopped: ${line#RECORD }. Move $RECORD away only if you want to start fresh. Nothing was changed." ;;
    *) die "Stopped: the download could not be checked. Nothing was installed." ;;
  esac
}

main() {
  CHANNEL="main"; FROM=""; SERVER=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --channel) [ $# -ge 2 ] || die "--channel needs a value"; CHANNEL=$2; shift 2 ;;
      --from) [ $# -ge 2 ] || die "--from needs a value"; FROM=$2; shift 2 ;;
      --server) SERVER=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "Unknown option: $1" ;;
    esac
  done
  # A speed bump, not a boundary: an agent with the same user id could run uv itself. It keeps honest agents out.
  if [ -n "${ORCH_GRANT:-}" ] || [ -n "${ORCH_SESSION:-}" ]; then
    die "The installer is for people, not for agents. Nothing was installed."
  fi
  # uv must behave the same everywhere: managed Python only, default index, hashes verified, no mirrors
  export UV_PYTHON_PREFERENCE=only-managed
  unset UV_PYTHON_DOWNLOADS_JSON_URL UV_PYTHON_INSTALL_MIRROR UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX UV_INDEX \
    UV_FIND_LINKS UV_NO_INDEX UV_NO_VERIFY_HASHES UV_INSECURE_HOST UV_INSECURE_NO_ZIP_VALIDATION \
    PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_NO_INDEX PIP_TRUSTED_HOST
  case "$FROM" in http:*) die "--from over plain http is not allowed: use https or a local folder. Nothing was installed." ;; esac
  printf '%s' "$CHANNEL" | grep -Eq '^(main|develop|test-[a-z0-9][a-z0-9-]{0,30})$' || die "Unknown channel: $CHANNEL"
  [ -n "${HOME:-}" ] || die "HOME is not set."
  for tool in curl tar mktemp uname sed grep cut tr; do
    command -v "$tool" >/dev/null 2>&1 || die "The tool '$tool' is missing. Install it and run the installer again."
  done
  command -v shasum >/dev/null 2>&1 || command -v sha256sum >/dev/null 2>&1 || die "Neither shasum nor sha256sum is available."

  find_platform
  if [ -n "$SERVER" ]; then check_linger; fi

  # where things live (see LAYOUT above)
  xdg_config=${XDG_CONFIG_HOME:-}
  xdg_data=${XDG_DATA_HOME:-}
  if [ -n "${ORCH_STATE_DIR:-}" ]; then STATE=$ORCH_STATE_DIR
  elif [ "${xdg_config#/}" != "$xdg_config" ]; then STATE="$xdg_config/orch"
  else STATE="$HOME/.config/orch"; fi
  case "$STATE" in /*) ;; *) die "ORCH_STATE_DIR must be an absolute path." ;; esac
  if [ "${xdg_data#/}" != "$xdg_data" ]; then DATA="$xdg_data/orch"; else DATA="$HOME/.local/share/orch"; fi
  BIN="$HOME/.local/bin"
  RECORD="$DATA/install.json"

  T=$(mktemp -d "${TMPDIR:-/tmp}/orch-install.XXXXXX")
  trap cleanup EXIT INT TERM
  mkdir -p "$T/files"
  write_verifier
  umask 077

  get_uv
  get_python

  say "Fetching the signed release information ($CHANNEL) ..."
  if [ -z "$FROM" ]; then
    fetch "https://github.com/$RELEASE_REPO/releases/download/channel-$CHANNEL/channel.json" "$T/channel.json" \
      || die "Could not reach the release server. Nothing was installed."
  elif [ -d "$FROM" ]; then
    [ -f "$FROM/channel.json" ] || die "$FROM has no channel.json. Nothing was installed."
    cp "$FROM/channel.json" "$T/channel.json"
  else
    fetch "${FROM%/}/channel.json" "$T/channel.json" || die "Could not reach $FROM. Nothing was installed."
  fi

  # 1. verify the signed manifest BEFORE anything of it is used
  "$PY" -I "$T/verify.py" check --channel "$CHANNEL" --envelope "$T/channel.json" --record "$RECORD" --json-out "$T/check.json" \
    > "$T/check.out" 2> "$T/check.err" || stop_unverified "$T/check.err"
  VERSION=$(sed -n 's/^VERSION'"$TAB"'//p' "$T/check.out")
  say "Release $VERSION is signed. Downloading its files ..."

  # 2. download ONLY the files the manifest lists, then check size and sha256 of each against it
  # shellcheck disable=SC2034 # role is part of the line format
  while IFS="$TAB" read -r kind role name size url; do
    [ "$kind" = FILE ] || continue
    if [ -z "$FROM" ]; then
      curl -fsSL --retry 2 --connect-timeout 20 --max-time 900 --proto '=https' --tlsv1.2 --max-filesize "$size" "$url" -o "$T/files/$name" </dev/null \
        || die "Could not download $name. Nothing was installed."
    elif [ -d "$FROM" ]; then
      [ -f "$FROM/$name" ] && [ ! -L "$FROM/$name" ] || die "$name is missing from $FROM. Nothing was installed."
      cp "$FROM/$name" "$T/files/$name"
    else
      fetch "${FROM%/}/$name" "$T/files/$name" || die "Could not download $name. Nothing was installed."
    fi
  done < "$T/check.out"
  "$PY" -I "$T/verify.py" check --channel "$CHANNEL" --envelope "$T/channel.json" --record "$RECORD" --files "$T/files" \
    --json-out "$T/check.json" > "$T/check2.out" 2> "$T/check.err" || stop_unverified "$T/check.err"

  # 3. install the verified local files (no URL is ever handed to uv for orch itself). Their dependencies DO come from
  #    the package index, but only the exact versions in the signed constraints, by hash, and only as wheels (--no-build:
  #    no setup.py or build backend of a dependency ever runs).
  mkdir -p "$STATE/install/tools" "$DATA"
  VDIR="$STATE/install/tools/orch-$VERSION"
  if [ ! -f "$VDIR/.complete" ]; then
    rm -rf "$VDIR"
    BUILT="$VDIR"
    mkdir -m 700 "$VDIR"
    say "Installing orch $VERSION ..."
    "$UV" venv --no-config --quiet --python "$PY" "$VDIR/venv" >&2 || die "Could not create the environment. Nothing was installed."
    "$PY" -I "$T/verify.py" reqs --files "$T/files" --check-json "$T/check.json" > "$T/requirements.txt"
    "$UV" pip install --no-config --quiet --require-hashes --no-build --no-deps --python "$VDIR/venv/bin/python" -r "$T/requirements.txt" >&2 \
      || die "Installing the verified files failed (a dependency may have no ready-made wheel). Nothing was installed."
    "$VDIR/venv/bin/orch" --version >/dev/null 2>&1 || die "The installed orch does not start. Nothing was installed."
    : > "$VDIR/.complete"
  fi

  # 4. record first, then flip (the A/B switch), the PATH shim and the host format
  "$PY" -I "$T/verify.py" record --record "$RECORD" --check-json "$T/check.json" --tool-dir "$VDIR" --uv "$UV" --uv-version "$UV_VERSION" \
    2>/dev/null || die "Could not write the install record ($RECORD). The version in use was not changed."
  "$PY" -I "$T/verify.py" flip --link "$STATE/install/current" --target "tools/orch-$VERSION" 2>/dev/null \
    || die "Could not switch to the new version. The version in use was not changed."
  BUILT=""
  if [ -e "$BIN/orch" ] && [ ! -L "$BIN/orch" ]; then
    note "Note: $BIN/orch is a file that orch did not create, so it was left alone. Run orch with $STATE/install/current/venv/bin/orch."
  else
    "$PY" -I "$T/verify.py" flip --link "$BIN/orch" --target "$STATE/install/current/venv/bin/orch" 2>/dev/null \
      || note "Could not create $BIN/orch. Run orch with $STATE/install/current/venv/bin/orch."
  fi
  "$PY" -I "$T/verify.py" hostformat --state "$STATE" --format "$HOST_FORMAT" 2>/dev/null \
    || note "Could not write $STATE/format.json; orch will say so when it starts."

  say "orch $VERSION is installed."
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "Add this line to your shell profile, then open a new terminal:  export PATH=\"$BIN:\$PATH\"" ;;
  esac
  if [ -n "$SERVER" ]; then
    say "Next, on this server:  orch setup --server"
  else
    say "Next:  orch setup"
  fi
  if [ "$(basename "${0:-}")" = install.sh ] && [ -f "${0:-}" ]; then say "This installer's sha256: $(sha256_of "$0")"; fi
}

main "$@"
