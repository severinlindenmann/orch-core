"""scripts/release/ceremony.py against fake `pass-cli` and `gh` executables. No real key, vault or GitHub is touched.

The fakes record argv and env to ``log/`` and keep their "vault" and "GitHub secret" in ``sim/``. ``sim/`` is the
only place a secret may appear (it stands for Proton Pass and GitHub); everything else under the test directory,
plus stdout and stderr, is scanned for every form of every seed."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.release.test_release_tooling import wheels

ROOT = Path(__file__).resolve().parents[4]
TOOLS = ROOT / "scripts" / "release"
sys.path.insert(0, str(TOOLS))

import ceremony  # noqa: E402
import floor  # noqa: E402
import manifest  # noqa: E402
import sign  # noqa: E402

T0 = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
REPO = "severinlindenmann/orch-core"

FAKE_COMMON = """\
import json, os, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent
SIM, LOG = D / "sim", D / "log"
argv = sys.argv[1:]
import stat
st = os.fstat(0)
DEVNULL = stat.S_ISCHR(st.st_mode) and st.st_rdev == os.stat("/dev/null").st_rdev
stdin = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()
with (LOG / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"tool": TOOL, "argv": argv, "env": dict(os.environ), "devnull": DEVNULL}) + "\\n")
with (SIM / f"{TOOL}-stdin.bin").open("ab") as f:  # stands for the vault / GitHub: the one place a secret may be
    f.write(stdin + b"\\n--\\n")


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
"""

FAKE_PASS = (
    'TOOL = "pass-cli"\n'
    + FAKE_COMMON
    + """
import hashlib
V = SIM / "vault"


def item_id(title):
    return "ID" + hashlib.sha1(title.encode()).hexdigest()[:20]


if argv[:1] == ["info"]:
    sys.exit(0)
if argv[:3] == ["item", "create", "login"]:
    if "--get-template" in argv:
        if (SIM / "pass-template-fail").exists():
            sys.exit(1)
        tpl = {"title": "", "username": "", "email": "", "password": "", "totp_uri": "", "urls": [], "extra_fields": []}
        print(json.dumps(tpl))
        sys.exit(0)
    item = json.loads(stdin)
    assert opt("--vault-name") == "Agents" and opt("--from-template") == "-"
    if os.environ.get("PROTON_PASS_AGENT_REASON", "") == "":
        sys.exit(9)
    (V / (item["title"] + ".json")).write_text(json.dumps(item))
    print("Item created")  # create's stdout is not parsed: the script must take the ID from the list
    sys.exit(0)
if argv[:2] == ["item", "list"]:
    assert opt("--vault-name") == "Agents" and opt("--output") == "json"
    if (SIM / "pass-list-fail").exists():
        sys.exit(3)
    want = opt("--filter-state") or "active"  # a run that forgets the filter never sees trashed items
    items = []
    for p in sorted(V.glob("*.json")):
        state = "trashed" if json.loads(p.read_text()).get("trashed") else "active"
        if state == want:
            items.append({"id": item_id(p.stem), "share_id": "S", "vault_id": "V", "state": state, "flags": [],
                          "create_time": "t", "modify_time": "t", "title": p.stem, "item_type": "login"})
    print(json.dumps({"items": items, "folders": []}))
    sys.exit(0)
if argv[:2] == ["item", "view"]:
    if (SIM / "pass-view-fail").exists() or "--item-title" in argv:
        sys.exit(3)
    for p in V.glob("*.json"):
        if item_id(p.stem) == opt("--item-id"):
            print(json.loads(p.read_text())[opt("--field")])
            sys.exit(0)
    sys.exit(1)
sys.exit(2)
"""
)

FAKE_GH = (
    'TOOL = "gh"\n'
    + FAKE_COMMON
    + """
if argv[:2] == ["auth", "status"]:
    sys.exit(0)
if argv[:1] == ["api"]:
    path = "user" if "user" in argv else argv[-1]
    if path == "user":
        if "--jq" in argv:
            print("owner")
            sys.exit(0)
        from email.utils import formatdate

        d = (SIM / "gh-date").read_text() if (SIM / "gh-date").exists() else formatdate(usegmt=True)
        print("HTTP/2.0 200 OK\\r\\nDate: " + d + "\\r\\n\\r\\n")
        print(json.dumps({"login": "owner"}))
        sys.exit(0)
    name = "env" if path.endswith("/release") else "policies"
    p = SIM / f"gh-{name}.json"
    if not p.exists():
        sys.exit(1)
    if "-i" in argv:
        from email.utils import formatdate

        d = (SIM / "gh-date").read_text() if (SIM / "gh-date").exists() else formatdate(usegmt=True)
        if not (SIM / "gh-nodate").exists():
            print("HTTP/2.0 200 OK\\r\\nDate: " + d + "\\r\\nContent-Type: application/json\\r\\n\\r\\n")
        else:
            print("HTTP/2.0 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n")
    print(p.read_text())
    sys.exit(0)
if argv[:2] == ["secret", "set"]:
    assert argv[2] == "ORCH_RELEASE_KEY" and opt("--env") == "release" and opt("--repo")
    (SIM / "gh-secret").write_bytes(stdin)
    sys.exit(0)
if argv[:2] == ["secret", "list"]:
    if (SIM / "gh-secret").exists():
        print("ORCH_RELEASE_KEY\\tUpdated just now")
    sys.exit(0)
sys.exit(2)
"""
)

GOOD_ENV = {
    "can_admins_bypass": False,
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}
GOOD_POLICIES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


class Lab:
    def __init__(self, base: Path):
        self.base = base
        for d in ("fake/bin", "fake/sim/vault", "fake/log", "repo/release", "tmp", "home"):
            (base / d).mkdir(parents=True)
        self.sim = base / "fake/sim"
        for name, body in (("pass-cli", FAKE_PASS), ("gh", FAKE_GH)):
            p = base / "fake/bin" / name
            p.write_text(f"#!{sys.executable}\n{body}")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.repo = base / "repo"
        for rel in (ceremony.KEYS_PY, ceremony.INSTALL_SH):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text((ROOT / rel).read_text())
        (self.repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
        self.unembed()
        self.original = self.snapshot()
        (self.sim / "gh-date").write_text("Mon, 02 Nov 2026 09:00:00 GMT")  # matches the patched clock T0
        (self.sim / "gh-env.json").write_text(json.dumps(GOOD_ENV))
        (self.sim / "gh-policies.json").write_text(json.dumps(GOOD_POLICIES))

    def unembed(self) -> None:
        """Start from the tree as it was before the ceremony: the real checkout has the production root embedded."""
        import re

        keys_py, sh, gi = self.repo / ceremony.KEYS_PY, self.repo / ceremony.INSTALL_SH, self.repo / ".gitignore"
        keys_py.write_text(
            re.sub(r"^PRODUCTION_ROOTS: Mapping\[str, bytes\] = \{.*?^\}", "PRODUCTION_ROOTS: Mapping[str, bytes] = {}",
                   keys_py.read_text(), flags=re.M | re.S)
        )  # fmt: skip
        sh.write_text(re.sub(r"^ROOTS_B64 = \{.*\}$", "ROOTS_B64 = {}", sh.read_text(), flags=re.M))
        gi.write_text("".join(x for x in gi.read_text().splitlines(True) if x.strip() != "release/keys/root.env"))
        self.original = self.snapshot()

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.base / 'fake/bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "TMPDIR": str(self.base / "tmp"),
            "HOME": str(self.base / "home"),
        }

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.repo)): p.read_bytes() for p in sorted(self.repo.rglob("*")) if p.is_file()}

    def calls(self) -> list[dict]:
        p = self.base / "fake/log/calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def vault(self, title: str) -> dict:
        return json.loads((self.sim / "vault" / f"{title}.json").read_text())

    def seeds(self) -> list[bytes]:
        out = []
        for p in (self.sim / "vault").glob("*.json"):
            out.append(base64.b64decode(json.loads(p.read_text())["password"]))
        return out

    def assert_no_leak(self, *streams: str) -> None:
        for seed in self.seeds():
            forms = {
                seed,
                base64.b64encode(seed),
                base64.urlsafe_b64encode(seed),
                seed.hex().encode(),
                base64.b64encode(seed).rstrip(b"="),
            }
            for s in streams:
                assert not any(f.decode(errors="ignore") in s for f in forms)
            for p in self.base.rglob("*"):
                if p.is_file() and self.sim not in p.parents:
                    data = p.read_bytes()
                    assert not any(f in data for f in forms), f"secret found in {p}"

    def tmp_left(self) -> list[str]:
        return [p.name for p in (self.base / "tmp").iterdir()]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    lab = Lab(tmp_path)
    for k, v in lab.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ceremony, "_now", lambda: T0)
    monkeypatch.setattr(ceremony, "_fail_at", None)
    return lab


def run_main(lab: Lab, capsys, *cmd: str) -> tuple[int, str, str]:
    rc = ceremony.main([*cmd, "--repo-root", str(lab.repo), "--gh-repo", REPO])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def embedded(lab: Lab):
    spec = importlib.util.spec_from_file_location("embedded_keys", lab.repo / ceremony.KEYS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def envelope_for(lab: Lab, tmp: Path, channel: str = "main") -> bytes:
    """A real manifest signed by the generated CI key with the generated cert and revocations."""
    ci_seed = base64.b64decode(lab.vault(ceremony.CI_TITLE)["password"])
    key = sign.Ed25519PrivateKey.from_private_bytes(ci_seed)
    keys = lab.repo / "release/keys"
    doc = manifest.build_manifest(
        channel=channel,
        version="2.0.0",
        seq=1,
        commit="a" * 40,
        issued_at="2026-11-02T10:00:00Z",
        expires_at="2026-11-20T10:00:00Z",
        files=wheels(tmp, "2.0.0"),
        base_url="https://example.invalid/releases/v2.0.0",
        changelog="TEST",
        min_from="2.0.0",
    )
    return sign.sign_envelope(
        manifest.jcs(doc),
        purpose="core",
        key=key,
        key_id="rk-1",
        key_cert=json.loads((keys / "rk-1.cert.json").read_text()),
        revocations=json.loads((keys / "revocations.json").read_text()),
    )


def verify(lab: Lab, monkeypatch, envelope: bytes, channel: str = "main", when: datetime = T0 + timedelta(days=1)):
    from orch.update import keys
    from orch.update.verify import verify_envelope

    monkeypatch.setattr(keys, "PRODUCTION_ROOTS", embedded(lab).PRODUCTION_ROOTS)  # the generated, embedded root
    return verify_envelope(
        envelope, purpose="core", channel=channel, now=when, seq_seen=None, revocations_issued_at=None
    )


# ---- the happy path ------------------------------------------------------------------------------------------------


def test_init_succeeds_without_leaking_and_prints_only_the_allowed_lines(lab, capsys):
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 0 and err == "", err
    pub = json.loads((lab.repo / "release/keys/root-1.pub.json").read_text())
    lines = out.splitlines()
    assert lines[:3] == [f"pass-cli: {lab.base}/fake/bin/pass-cli", f"gh: {lab.base}/fake/bin/gh", "gh account: owner"]
    lines = lines[3:]
    assert [x.split(":")[0] for x in lines[:2]] == ["proton item id (root)", "proton item id (ci)"]
    assert lines[2:5] == [
        "root key id: root-1",
        f"fingerprint: {pub['fingerprint']}",
        "issued_at: 2026-11-02T09:00:00Z",
    ]
    assert pub["fingerprint"].startswith("sha256:") and len(pub["fingerprint"]) == 71
    assert lines[5] == "changed files:"
    assert lines[-1] == "ORCH_RELEASE_KEY in environment release: present"
    changed = {x.strip() for x in lines[6:-1]}
    assert changed == {
        ".gitignore",
        "install.sh",
        ceremony.KEYS_PY,
        "release/keys/revocations.json",
        "release/keys/rk-1.cert.json",
        "release/keys/root-1.pub.json",
        "release/keys/root.env",
    }
    assert lab.tmp_left() == []
    lab.assert_no_leak(out, err)
    assert "release/keys/root.env" in (lab.repo / ".gitignore").read_text().splitlines()
    assert (
        lab.repo / "release/keys/root.env"
    ).read_text() == "ORCH_ROOT_SIGNING_KEY=pass://Agents/orch release root key/password\n"


def test_each_tool_gets_only_its_own_env_allowlist(lab, capsys, monkeypatch):
    noise = [
        "GH_TOKEN", "GH_CONFIG_DIR", "PROTON_PASS_X", "HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        "RUST_LOG", "GH_DEBUG", "PYTHONPATH", "LD_PRELOAD", "SOME_SECRET",
    ]  # fmt: skip
    for k in noise:
        monkeypatch.setenv(k, "/x")
    assert run_main(lab, capsys, "init")[0] == 0
    base = {"HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "PATH", "__CF_USER_TEXT_ENCODING"}
    seen = {"pass-cli": set(), "gh": set()}
    for c in lab.calls():
        seen[c["tool"]] |= set(c["env"])
    assert seen["gh"] <= base | {"GH_TOKEN", "GH_CONFIG_DIR"} and {"GH_TOKEN", "GH_CONFIG_DIR"} <= seen["gh"]
    assert seen["pass-cli"] <= base | {"PROTON_PASS_X", "PROTON_PASS_AGENT_REASON"}
    assert {"PROTON_PASS_X", "PROTON_PASS_AGENT_REASON"} <= seen["pass-cli"]


def test_the_binaries_are_resolved_once_to_absolute_paths(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    assert set(ceremony._BINS) == {"pass-cli", "gh"} and all(os.path.isabs(p) for p in ceremony._BINS.values())
    assert ceremony._BINS["gh"] == str(lab.base / "fake/bin/gh")


def test_a_missing_tool_refuses(lab, capsys, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "not found on PATH" in err and lab.tmp_left() == []


def test_init_secrets_only_travel_on_stdin_and_the_env_is_sanitized(lab, capsys, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 0
    calls = lab.calls()
    seeds = lab.seeds()
    assert len(seeds) == 2
    for c in calls:
        assert "PYTHONPATH" not in c["env"] and "LD_PRELOAD" not in c["env"]
        if c["tool"] == "pass-cli":
            assert c["env"]["PROTON_PASS_AGENT_REASON"]
    sets = [c for c in calls if c["tool"] == "gh" and c["argv"][:2] == ["secret", "set"]]
    assert len(sets) == 1 and sets[0]["argv"] == [
        "secret",
        "set",
        "ORCH_RELEASE_KEY",
        "--env",
        "release",
        "--repo",
        REPO,
    ]
    # the GitHub secret is the CI key's seed (stdin), which is also what Proton holds for rk-1
    assert (lab.sim / "gh-secret").read_text() == lab.vault(ceremony.CI_TITLE)["password"]
    creates = [c for c in calls if c["tool"] == "pass-cli" and "--from-template" in c["argv"]]
    assert [c["argv"][-1] for c in creates] == ["-", "-"]
    assert lab.vault(ceremony.ROOT_TITLE)["username"] == "root-1"
    assert lab.vault(ceremony.CI_TITLE)["username"] == "rk-1"
    keys = {"title", "username", "email", "password", "totp_uri", "urls", "extra_fields"}
    assert set(lab.vault(ceremony.ROOT_TITLE)) == keys
    assert lab.vault(ceremony.ROOT_TITLE)["totp_uri"] == "" and lab.vault(ceremony.ROOT_TITLE)["urls"] == []
    lab.assert_no_leak(out, err)


def test_init_runs_under_python_dash_I_as_a_real_process(lab):
    (lab.sim / "gh-date").unlink()  # the real clock runs here: the fake answers with the real time
    p = subprocess.run(
        [sys.executable, "-I", str(TOOLS / "ceremony.py"), "init", "--repo-root", str(lab.repo), "--gh-repo", REPO],
        env=lab.env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr
    assert p.stderr == ""
    assert "fingerprint: sha256:" in p.stdout
    assert lab.tmp_left() == []
    lab.assert_no_leak(p.stdout, p.stderr)


def test_public_outputs_verify_with_the_embedded_root_and_main_only(lab, capsys, monkeypatch, tmp_path_factory):
    assert run_main(lab, capsys, "init")[0] == 0
    cert_obj = json.loads((lab.repo / "release/keys/rk-1.cert.json").read_text())
    cert = json.loads(base64.b64decode(cert_obj["cert"]))
    assert cert["channels"] == ["main"] and cert["purpose"] == "core" and cert["serial"] == 1
    assert cert["key_id"] == "rk-1"
    nb = datetime.strptime(cert["not_before"], "%Y-%m-%dT%H:%M:%SZ")
    na = datetime.strptime(cert["not_after"], "%Y-%m-%dT%H:%M:%SZ")
    assert na - nb == timedelta(days=180)
    env = envelope_for(lab, tmp_path_factory.mktemp("w"))
    verify(lab, monkeypatch, env)  # raises UpdateRefused unless the whole chain (root, cert, list, signature) is good
    from orch.update.verify import UpdateRefused

    # not allowed on any other channel: re-sign a develop manifest by hand and expect the verifier to refuse it
    with pytest.raises(ValueError):  # sign.py itself refuses (the certificate does not allow the channel)
        envelope_for(lab, tmp_path_factory.mktemp("w2"), channel="develop")
    # the embedded roots in install.sh and keys.py are the same key
    sh = (lab.repo / "install.sh").read_text()
    pub = json.loads((lab.repo / "release/keys/root-1.pub.json").read_text())["public_key"]
    assert f'ROOTS_B64 = {{"root-1": "{pub}"}}' in sh
    assert embedded(lab).PRODUCTION_ROOTS == {"root-1": base64.b64decode(pub)}
    assert pub not in (ROOT / "install.sh").read_text()
    with pytest.raises(UpdateRefused):  # and a verifier without the embedded root refuses (production roots stay empty)
        from orch.update.verify import verify_envelope

        verify_envelope(
            env,
            purpose="core",
            channel="main",
            now=T0 + timedelta(days=1),
            seq_seen=None,
            revocations_issued_at=None,
            roots={},
        )


def test_init_floor_equals_the_revocation_list(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    sh = (lab.repo / "install.sh").read_text()
    assert (
        floor.read_floor(sh)
        == floor.read_list(lab.repo / "release/keys/revocations.json")
        == ("2026-11-02T09:00:00Z", 1)
    )
    assert floor.main(["--check", str(lab.repo / "install.sh"), str(lab.repo / "release/keys/revocations.json")]) == 0


def test_the_leak_scan_really_detects_a_leak(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    seed = lab.seeds()[0]
    with pytest.raises(AssertionError):
        lab.assert_no_leak(base64.b64encode(seed).decode())
    stray = lab.base / "home" / "stray.txt"
    stray.write_bytes(b"x" + seed.hex().encode())
    with pytest.raises(AssertionError):
        lab.assert_no_leak()


# ---- refusals ------------------------------------------------------------------------------------------------------


def _assert_refused_cleanly(lab, capsys, expect: str):
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "fingerprint" not in out and "proton item id" not in out and expect in err
    assert lab.snapshot() == lab.original
    assert lab.tmp_left() == []
    forbidden = [c for c in lab.calls() if "--from-template" in c["argv"] or c["argv"][:2] == ["secret", "set"]]
    assert forbidden == []
    lab.assert_no_leak(out, err)


def test_refuses_when_the_root_item_already_exists(lab, capsys):
    (lab.sim / "vault" / f"{ceremony.ROOT_TITLE}.json").write_text(
        json.dumps({"title": "x", "username": "u", "password": "AAAA"})
    )
    _assert_refused_cleanly(lab, capsys, "already exists")


def test_refuses_when_the_ci_item_already_exists(lab, capsys):
    (lab.sim / "vault" / f"{ceremony.CI_TITLE}.json").write_text(
        json.dumps({"title": "x", "username": "u", "password": "AAAA"})
    )
    _assert_refused_cleanly(lab, capsys, "already exists")


@pytest.mark.parametrize("flag", ["pass-list-fail", "pass-view-fail"])
def test_an_error_from_pass_cli_is_never_read_as_absent(lab, capsys, flag):
    (lab.sim / flag).write_text("")
    if flag == "pass-view-fail":  # view is not used for the existence check at all: init must go through
        rc, out, _ = run_main(lab, capsys, "init")  # ... but the read-back by ID then fails, without a title fallback
        assert rc == 1 and "proton item id" not in out  # the ID is printed only after a successful read-back
        assert not any("--item-title" in c["argv"] for c in lab.calls())
        return
    _assert_refused_cleanly(lab, capsys, "refusing")


@pytest.mark.parametrize(
    "answer",
    [
        b"not json",
        b"null",
        b"[]",
        b"{}",
        b'{"error": "vault is locked"}',
        b'{"items": null}',
        b'{"items": [{"metadata": {"name": "orch release root key"}, "id": "X", "state": "active"}]}',
        b'{"items": [{"id": "X", "title": "t"}]}',
        b'{"items": [{"id": 5, "title": "t", "state": "active"}]}',
    ],
)
def test_any_other_list_shape_refuses_even_with_exit_0(lab, capsys, monkeypatch, answer):
    real = ceremony.run
    list_cmd = ["pass-cli", "item", "list"]
    monkeypatch.setattr(ceremony, "run", lambda a, i=None, **k: (0, answer) if a[:3] == list_cmd else real(a, i, **k))
    _assert_refused_cleanly(lab, capsys, "unexpected answer")


def test_a_trashed_root_item_also_blocks_init(lab, capsys):
    (lab.sim / "vault" / f"{ceremony.ROOT_TITLE}.json").write_text(
        json.dumps({"title": "x", "username": "u", "password": "AAAA", "trashed": True})
    )
    _assert_refused_cleanly(lab, capsys, "already exists")


def test_the_list_is_asked_for_active_and_trashed(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    states = [
        c["argv"][c["argv"].index("--filter-state") + 1] for c in lab.calls() if c["argv"][:2] == ["item", "list"]
    ]
    assert states[:2] == ["active", "trashed"]


@pytest.mark.parametrize("extra", [[], ["a", "b"]], ids=["none-found", "two-found"])
def test_not_exactly_one_item_after_create_refuses_and_prints_no_id(lab, capsys, monkeypatch, extra):
    real, calls = ceremony._pass_items, []

    def listing():
        calls.append(1)
        items = real()
        if len(calls) == 1:
            return items  # preflight
        title = ceremony.ROOT_TITLE
        return [x for x in items if x[0] != title] + [(title, f"DUP{n}", "active") for n in extra]

    monkeypatch.setattr(ceremony, "_pass_items", listing)
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "proton item id" not in out and "expected exactly 1" in err
    assert lab.tmp_left() == [] and lab.snapshot() == lab.original
    assert not any(c["argv"][:2] == ["item", "view"] and "--field" in c["argv"] for c in lab.calls())


def test_the_printed_id_is_the_one_from_the_list(lab, capsys):
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 0
    ids = dict(x.split(": ") for x in out.splitlines() if x.startswith("proton item id"))
    assert ids["proton item id (root)"].startswith("ID") and ids["proton item id (ci)"].startswith("ID")
    assert ids["proton item id (root)"] != ids["proton item id (ci)"]


@pytest.mark.parametrize("extra", [{"note": "n"}, {"mystery": 1}, {"urls": ["https://x"]}, {"extra_fields": [1]}])
def test_template_keys_outside_ours_must_be_empty(lab, capsys, monkeypatch, extra):
    real = ceremony.run
    base = {"title": "", "username": "", "email": "", "password": "", "totp_uri": "", "urls": []}
    tpl = json.dumps({**base, **extra}).encode()
    monkeypatch.setattr(ceremony, "run", lambda a, i=None, **k: (0, tpl) if "--get-template" in a else real(a, i, **k))
    _assert_refused_cleanly(lab, capsys, "unexpected non-empty")


def test_every_call_has_stdin_closed_except_the_ones_that_feed_it(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    feeding = [c for c in lab.calls() if not c["devnull"]]
    assert {tuple(c["argv"][:3]) for c in feeding} == {
        ("item", "create", "login"),
        ("secret", "set", "ORCH_RELEASE_KEY"),
    }
    assert all("--from-template" in c["argv"] or c["tool"] == "gh" for c in feeding)
    gt = [c for c in lab.calls() if "--get-template" in c["argv"]]
    assert gt and all(c["devnull"] for c in gt)  # the call that hung when it inherited a terminal


def test_template_unreadable_aborts_before_anything_is_stored(lab, capsys):
    (lab.sim / "pass-template-fail").write_text("")
    _assert_refused_cleanly(lab, capsys, "template")


def test_template_with_unexpected_filled_fields_aborts(lab, capsys, monkeypatch):
    real = ceremony.run
    tpl = json.dumps({"title": "", "username": "", "password": "", "totp_uri": "otpauth://x"}).encode()
    monkeypatch.setattr(ceremony, "run", lambda a, i=b"", **k: (0, tpl) if "--get-template" in a else real(a, i, **k))
    _assert_refused_cleanly(lab, capsys, "unexpected non-empty")


def test_readback_and_resign_use_the_item_id_never_the_title(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    assert run_main(lab, capsys, "resign-revocations")[0] == 0
    views = [c for c in lab.calls() if c["tool"] == "pass-cli" and c["argv"][:2] == ["item", "view"]]
    assert len(views) == 3 and all("--item-id" in c["argv"] and "--item-title" not in c["argv"] for c in views)
    assert not any("--item-title" in c["argv"] for c in lab.calls())


def test_refuses_when_the_release_environment_is_missing(lab, capsys):
    (lab.sim / "gh-env.json").unlink()
    _assert_refused_cleanly(lab, capsys, "does not exist")


@pytest.mark.parametrize(
    "policies",
    [
        {"branch_policies": []},
        {"branch_policies": [{"name": "develop", "type": "branch"}]},
        {"branch_policies": [{"name": "main", "type": "branch"}, {"name": "develop", "type": "branch"}]},
        {"branch_policies": [{"name": "main", "type": "tag"}]},
        {"branch_policies": [{"name": "*", "type": "branch"}]},
    ],
)
def test_refuses_when_the_policy_is_not_main_only(lab, capsys, policies):
    (lab.sim / "gh-policies.json").write_text(json.dumps(policies))
    _assert_refused_cleanly(lab, capsys, "not exactly: main")


@pytest.mark.parametrize(
    "env",
    [
        {"deployment_branch_policy": None},
        {"deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}},
        {},
    ],
)
def test_refuses_when_the_environment_has_no_custom_branch_policy(lab, capsys, env):
    (lab.sim / "gh-env.json").write_text(json.dumps({"can_admins_bypass": False, **env}))
    _assert_refused_cleanly(lab, capsys, "custom branch policy")


@pytest.mark.parametrize("date", ["Mon, 02 Nov 2026 09:06:00 GMT", "Mon, 02 Nov 2026 08:54:00 GMT", "garbage"])
def test_refuses_a_clock_that_differs_from_githubs(lab, capsys, date):
    (lab.sim / "gh-date").write_text(date)
    _assert_refused_cleanly(lab, capsys, "Date header" if date == "garbage" else "clock differs")


def test_a_small_clock_difference_is_fine(lab, capsys):
    (lab.sim / "gh-date").write_text("Mon, 02 Nov 2026 09:04:00 GMT")
    assert run_main(lab, capsys, "init")[0] == 0


def test_refuses_when_github_sends_no_date(lab, capsys):
    (lab.sim / "gh-nodate").write_text("")
    _assert_refused_cleanly(lab, capsys, "Date header")


@pytest.mark.parametrize("bypass", [True, "yes", None, "missing"])
def test_refuses_unless_admins_bypass_is_present_and_false(lab, capsys, bypass):
    env = {k: v for k, v in GOOD_ENV.items() if k != "can_admins_bypass"}
    if bypass != "missing":
        env["can_admins_bypass"] = bypass
    (lab.sim / "gh-env.json").write_text(json.dumps(env))
    _assert_refused_cleanly(lab, capsys, "can_admins_bypass")


def test_refuses_to_run_init_twice(lab, capsys):
    assert run_main(lab, capsys, "init")[0] == 0
    (lab.sim / "gh-secret").unlink()
    for p in (lab.sim / "vault").glob("*.json"):
        p.unlink()
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "already exists" in err and "fingerprint" not in out


# ---- cleanup after an injected failure at each step ---------------------------------------------------------------

INIT_STEPS = ["preflight", "root", "ci-key", "ci-proton", "ci-secret", "revocations", "stage", "write", "secret-list"]


@pytest.mark.parametrize("step", INIT_STEPS)
def test_init_cleans_up_after_a_failure_at_each_step(lab, capsys, monkeypatch, step):
    monkeypatch.setattr(ceremony, "_fail_at", step)
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and f"injected failure at {step}" in err
    assert lab.tmp_left() == []
    if step in INIT_STEPS[: INIT_STEPS.index("write")]:
        assert lab.snapshot() == lab.original  # nothing public is written before every external step has worked
    lab.assert_no_leak(out, err)


def test_temp_dir_is_removed_on_a_signal(lab, monkeypatch, capsys):
    import signal

    def boom(*_a, **_k):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(ceremony, "cmd_init", boom)
    old = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as e:
            run_main(lab, capsys, "init")
    finally:
        signal.signal(signal.SIGTERM, old)
    assert e.value.code == 128 + signal.SIGTERM
    assert lab.tmp_left() == []


def test_failing_children_do_not_echo_their_output(lab, capsys, monkeypatch):
    real = ceremony.run

    def noisy(argv, stdin=b"", **kw):
        rc, out = real(argv, stdin, **kw)
        if argv[:2] == ["gh", "secret"] and argv[2] == "set":
            return 1, b"SUPERSECRET-OUTPUT"
        return rc, out

    monkeypatch.setattr(ceremony, "run", noisy)
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "SUPERSECRET-OUTPUT" not in out + err and "could not set" in err
    assert lab.tmp_left() == []


# ---- resign-revocations --------------------------------------------------------------------------------------------


def _init_then(lab, capsys, monkeypatch, later: timedelta = timedelta(days=14)):
    assert run_main(lab, capsys, "init")[0] == 0
    capsys.readouterr()
    monkeypatch.setattr(ceremony, "_now", lambda: T0 + later)
    from email.utils import format_datetime

    (lab.sim / "gh-date").write_text(format_datetime(T0 + later, usegmt=True))  # GitHub's clock moves with ours


def _list(path: Path) -> dict:
    return json.loads(base64.b64decode(json.loads(path.read_text())["doc"]))


def test_resign_keeps_serials_raises_issued_at_and_bumps_the_floor(lab, capsys, monkeypatch, tmp_path_factory):
    _init_then(lab, capsys, monkeypatch)
    revs = lab.repo / "release/keys/revocations.json"
    before = _list(revs)
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 0 and err == "", err
    after = _list(revs)
    assert after["min_serial"] == before["min_serial"] and after["revoked"] == before["revoked"]
    assert after["issued_at"] == "2026-11-16T09:00:00Z" > before["issued_at"]
    lines = out.splitlines()
    assert lines[2] == "gh account: owner"
    assert lines[3] == "re-signing: min_serial 1, revoked []"
    assert lines[4] == "issued_at: 2026-11-16T09:00:00Z"
    assert {x.strip() for x in lines[6:]} == {"install.sh", "release/keys/revocations.json"}
    sh = (lab.repo / "install.sh").read_text()
    assert floor.read_floor(sh) == floor.read_list(revs) == ("2026-11-16T09:00:00Z", 1)  # manager condition 4
    assert floor.main(["--check", str(lab.repo / "install.sh"), str(revs)]) == 0
    assert lab.tmp_left() == []
    lab.assert_no_leak(out, err)
    # the new list is signed by the same root: an envelope built with it verifies against the embedded root
    env = envelope_for(lab, tmp_path_factory.mktemp("w"))
    verify(lab, monkeypatch, env, when=T0 + timedelta(days=15))


def test_resign_keeps_revoked_serials(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    # an earlier list that already revoked serial 7 with min_serial 3 (signed by the real generated root)
    import rootkey

    root = rootkey.Ed25519PrivateKey.from_private_bytes(base64.b64decode(lab.vault(ceremony.ROOT_TITLE)["password"]))
    old = rootkey.revoke(
        root, "root-1", rootkey.build_revocations(min_serial=3, revoked=[7, 5], issued_at="2026-11-02T09:00:00Z")
    )
    (lab.repo / "release/keys/revocations.json").write_text(json.dumps(old))
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 0
    got = _list(lab.repo / "release/keys/revocations.json")
    assert (got["min_serial"], got["revoked"]) == (3, [5, 7])
    assert floor.read_floor((lab.repo / "install.sh").read_text()) == ("2026-11-16T09:00:00Z", 3)


def test_resign_refuses_a_clock_that_is_not_after_the_current_list(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch, later=timedelta(0))
    snap = lab.snapshot()
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 1 and "not after" in err and lab.snapshot() == snap and lab.tmp_left() == []


@pytest.mark.parametrize("date", ["Mon, 16 Nov 2026 09:07:00 GMT", "Mon, 16 Nov 2026 08:53:00 GMT", "nonsense"])
def test_resign_refuses_a_clock_that_differs_from_githubs_before_reading_the_root(lab, capsys, monkeypatch, date):
    _init_then(lab, capsys, monkeypatch)
    (lab.sim / "gh-date").write_text(date)
    snap = lab.snapshot()
    n = _root_reads(lab)
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 1 and ("clock differs" in err or "Date header" in err)
    assert lab.snapshot() == snap and _root_reads(lab) == n and lab.tmp_left() == []


def test_resign_accepts_a_small_clock_difference(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    (lab.sim / "gh-date").write_text("Mon, 16 Nov 2026 09:03:00 GMT")
    assert run_main(lab, capsys, "resign-revocations")[0] == 0


def test_resign_refuses_a_root_that_is_not_the_published_one(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    other = base64.b64encode(bytes(range(32))).decode()
    p = lab.sim / "vault" / f"{ceremony.ROOT_TITLE}.json"
    item = json.loads(p.read_text())
    item["password"] = other
    p.write_text(json.dumps(item))
    snap = lab.snapshot()
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 1 and "not the embedded root" in err and lab.snapshot() == snap
    assert other not in out + err and lab.tmp_left() == []


@pytest.mark.parametrize("step", ["preflight", "read-root", "sign", "stage", "write"])
def test_resign_cleans_up_after_a_failure_at_each_step(lab, capsys, monkeypatch, step):
    _init_then(lab, capsys, monkeypatch)
    snap = lab.snapshot()
    monkeypatch.setattr(ceremony, "_fail_at", step)
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 1 and lab.tmp_left() == [] and lab.snapshot() == snap
    lab.assert_no_leak(out, err)


def test_resign_reads_the_root_from_proton_and_never_creates_anything(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    n = len(lab.calls())
    assert run_main(lab, capsys, "resign-revocations")[0] == 0
    new = lab.calls()[n:]
    assert [c["argv"][:2] for c in new if c["tool"] == "pass-cli"] == [
        ["info"],
        ["item", "list"],
        ["item", "list"],
        ["item", "view"],
    ]
    gh = [c["argv"] for c in new if c["tool"] == "gh"]  # only read-only identity / clock calls, never a secret call
    assert gh == [["api", "user", "--jq", ".login"], ["api", "-i", "user"]]
    first_view = next(i for i, c in enumerate(new) if c["argv"][:2] == ["item", "view"])
    assert new.index(next(c for c in new if c["argv"] == ["api", "-i", "user"])) < first_view  # clock before the root
    assert not any("--item-title" in c["argv"] for c in lab.calls())
    assert new[first_view]["argv"][-2:] == ["--field", "password"]


# ---- the current list is verified before the root key is fetched ---------------------------------------------------


def _root_reads(lab: Lab) -> int:
    return sum(1 for c in lab.calls() if c["tool"] == "pass-cli" and "--field" in c["argv"])


def _refused(lab, capsys, expect: str, *, root_read: bool = False):
    snap = lab.snapshot()
    n = _root_reads(lab)
    rc, out, err = run_main(lab, capsys, "resign-revocations")
    assert rc == 1 and expect in err and lab.snapshot() == snap and lab.tmp_left() == []
    assert (_root_reads(lab) > n) is root_read  # the Proton root is read only in the last case
    lab.assert_no_leak(out, err)


def _write_env(lab, doc: dict, root_seed: bytes | None = None):
    import rootkey

    seed = root_seed or base64.b64decode(lab.vault(ceremony.ROOT_TITLE)["password"])
    env = rootkey.revoke(rootkey.Ed25519PrivateKey.from_private_bytes(seed), "root-1", doc)
    (lab.repo / "release/keys/revocations.json").write_text(json.dumps(env))


def test_resign_refuses_a_tampered_doc_with_the_old_signature(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    import rootkey

    path = lab.repo / "release/keys/revocations.json"
    old = json.loads(path.read_text())
    evil = rootkey.build_revocations(min_serial=1, revoked=[], issued_at="2026-11-02T09:00:01Z")
    old["doc"] = base64.b64encode(manifest.jcs(evil)).decode()
    path.write_text(json.dumps(old))
    _refused(lab, capsys, "not a validly signed list")


def test_resign_refuses_a_bare_doc(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    doc = {"format": "orch.revocations/1", "min_serial": 1, "revoked": [], "issued_at": "2026-11-02T09:00:00Z"}
    (lab.repo / "release/keys/revocations.json").write_text(json.dumps(doc))
    _refused(lab, capsys, "not a validly signed list")


def test_resign_refuses_a_valid_signature_from_another_root(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    import rootkey

    doc = rootkey.build_revocations(min_serial=1, revoked=[], issued_at="2026-11-02T09:00:00Z")
    _write_env(lab, doc, root_seed=bytes(range(32)))
    _refused(lab, capsys, "not a validly signed list")


def test_resign_refuses_a_list_below_the_floor(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    import rootkey

    sh = lab.repo / "install.sh"
    sh.write_text(floor.bumped(sh.read_text(), "2026-11-02T09:00:00Z", 5))  # the floor moved on: min_serial 5
    doc = rootkey.build_revocations(min_serial=1, revoked=[], issued_at="2026-11-02T09:00:00Z")
    _write_env(lab, doc)  # genuinely signed by the root, but a lower min_serial than the floor
    _refused(lab, capsys, "below the floor")


def test_resign_refuses_a_proton_root_that_differs_from_the_embedded_one_and_signs_nothing(lab, capsys, monkeypatch):
    _init_then(lab, capsys, monkeypatch)
    p = lab.sim / "vault" / f"{ceremony.ROOT_TITLE}.json"
    item = json.loads(p.read_text())
    item["password"] = base64.b64encode(bytes(range(32))).decode()
    p.write_text(json.dumps(item))
    called = []
    monkeypatch.setattr(ceremony.rootkey, "revoke", lambda *a, **k: called.append(1))
    _refused(lab, capsys, "not the embedded root", root_read=True)
    assert called == []


# ---- the tree is never left half written ---------------------------------------------------------------------------


def _fail_replace_on(monkeypatch, n: int):
    real, count = os.replace, []

    def flaky(a, b):
        count.append(1)
        if len(count) == n:
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)


@pytest.mark.parametrize("n", [1, 3, 5, 6, 7])
def test_a_failing_write_restores_the_tree_and_init_can_be_rerun(lab, capsys, monkeypatch, n):
    _fail_replace_on(monkeypatch, n)
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1
    assert lab.snapshot() == lab.original  # byte-identical: no half-written file, no *.ceremony-tmp, no new file
    assert not (lab.repo / "release").exists() or not any((lab.repo / "release").rglob("*"))
    assert lab.tmp_left() == []
    lab.assert_no_leak(out, err)
    # recovery by hand as in the runbook: delete the Proton items, then preflight and the whole run pass again
    monkeypatch.undo()
    for p in (lab.sim / "vault").glob("*.json"):
        p.unlink()
    (lab.sim / "gh-secret").unlink()
    for k, v in lab.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ceremony, "_now", lambda: T0)
    assert run_main(lab, capsys, "init")[0] == 0


def test_the_keys_files_are_written_before_the_embeds(lab, capsys, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(
        os, "replace", lambda a, b: (seen.append(Path(b).relative_to(lab.repo).as_posix()), real(a, b))[1]
    )
    assert run_main(lab, capsys, "init")[0] == 0
    keys = [i for i, r in enumerate(seen) if r.startswith("release/keys/")]
    embeds = [i for i, r in enumerate(seen) if not r.startswith("release/keys/")]
    assert max(keys) < min(embeds) and len(seen) == 7


def test_when_restoring_fails_too_the_message_names_the_files(lab, capsys, monkeypatch):
    _fail_replace_on(monkeypatch, 6)  # .gitignore was already replaced, install.sh fails
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("read-only")))
    rc, out, err = run_main(lab, capsys, "init")
    assert rc == 1 and "restoring it failed for: .gitignore" in err
    assert not list(lab.repo.rglob("*.ceremony-tmp"))
    assert lab.tmp_left() == []
