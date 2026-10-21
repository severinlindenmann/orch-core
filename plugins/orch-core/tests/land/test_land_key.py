"""Landing task 4 (#596): the deploy key, the host-key pin, the pinned executables, and the two network operations.

No sshd here: a fake ``ssh`` (a shell script the TEST puts in a temporary directory, past the root-owned check by a
test-only seam) serves the remote's own ``git-upload-pack``/``git-receive-pack`` from a local bare repository and
records how it was called, so the real git runs the real fetch/push through our real ``GIT_SSH_COMMAND``.
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orch import gitsafe
from orch.land import key as lk
from orch.land.git import Candidate, LandGitError, LandRepo
from orch.land.key import KeyStore, LandKeyError
from orch.land.remote import fetch_target, push_candidate
from tests.land.conftest import dev_git
from tests.land.test_land_git import World, sh

HOST = "example.com"
MARK = "TOPSECRETMARKER"
REAL_GIT = "/usr/bin/git"

needs_system_git = pytest.mark.skipif(not os.path.isfile(REAL_GIT), reason="no /usr/bin/git")


@pytest.fixture
def state():
    """A host-state directory under HOME (custody refuses /tmp); deleted by its exact path."""
    d = Path(tempfile.mkdtemp(prefix=".lk-", dir=Path.home())).resolve()
    d.chmod(0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(state):
    return KeyStore(state, "ws-1")


def host_line(host: str = HOST) -> tuple[str, str]:
    pub = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    t, b = pub.split()[:2]
    return f"{host} {t} {b}\n", b


# -- the key (I8) ------------------------------------------------------------------------------------------------------


def test_generate_makes_a_private_ed25519_key_and_exports_only_the_public_half(store, state):
    pub = store.generate()
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" orch-land") and store.public_key() == pub
    ws = next((state / "land").iterdir())
    assert stat.S_IMODE(os.stat(state / "land").st_mode) == 0o700 and stat.S_IMODE(os.stat(ws).st_mode) == 0o700
    key = ws / "key"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and os.stat(key).st_nlink == 1
    assert key.read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert [p.name for p in ws.iterdir()] == ["key"]  # no temp left behind, no second copy of the public half
    # ssh itself agrees on the public key
    out = subprocess.run(["/usr/bin/ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, check=True)
    assert out.stdout.split()[:2] == pub.split()[:2]


def test_generate_never_overwrites_without_replace(store):
    first = store.generate()
    with pytest.raises(LandKeyError) as e:
        store.generate()
    assert e.value.code == "land.key_exists" and store.public_key() == first
    assert store.generate(replace=True) != first


def test_keys_are_per_workspace(state):
    a, b = KeyStore(state, "ws-a"), KeyStore(state, "ws-b")
    assert a.generate() != b.generate()
    assert len(list((state / "land").iterdir())) == 2


@pytest.mark.parametrize("workspace", ["", "a\0b", None])
def test_bad_workspace_id_is_refused(state, workspace):
    with pytest.raises(LandKeyError):
        KeyStore(state, workspace)  # type: ignore[arg-type]


def test_workspace_id_never_forms_a_path(state):
    s = KeyStore(state, "../../etc")
    s.generate()
    assert [p.name for p in (state / "land").iterdir()] and not (state.parent / "etc").exists()


def test_no_key_yet(store):
    with pytest.raises(LandKeyError) as e:
        store.public_key()
    assert e.value.code == "land.no_key"


def test_a_loose_key_file_is_refused(store, state):
    store.generate()
    key = next((state / "land").iterdir()) / "key"
    key.chmod(0o640)
    with pytest.raises(LandKeyError) as e:
        store.public_key()
    assert e.value.code == "land.key_unsafe"
    key.chmod(0o600)
    os.link(key, key.with_name("alias"))  # a second link: another name could be rewritten later
    with pytest.raises(LandKeyError):
        store.public_key()


def test_a_symlinked_key_file_is_refused(store, state):
    store.generate()
    key = next((state / "land").iterdir()) / "key"
    real = key.with_name("real")
    key.rename(real)
    key.symlink_to(real)
    with pytest.raises(LandKeyError) as e:
        store.public_key()
    assert e.value.code == "land.key_unsafe"


def test_a_group_writable_directory_is_refused(store, state):
    store.generate()
    next((state / "land").iterdir()).chmod(0o770)
    with pytest.raises(LandKeyError) as e:
        store.public_key()
    assert e.value.code == "land.key_unsafe"


def test_a_symlinked_land_directory_is_refused(state):
    elsewhere = state / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (state / "land").symlink_to(elsewhere)
    with pytest.raises(LandKeyError) as e:
        KeyStore(state, "ws").generate()
    assert e.value.code == "land.key_unsafe"
    assert list(elsewhere.iterdir()) == []


def test_errors_and_reprs_never_carry_key_material(store, state):
    store.generate()
    key = next((state / "land").iterdir()) / "key"
    secret = key.read_text()
    key.write_text(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{MARK}\n-----END OPENSSH PRIVATE KEY-----\n")
    key.chmod(0o600)
    with pytest.raises(LandKeyError) as e:
        store.public_key()
    text = "".join(traceback.format_exception(e.value))
    for t in (str(e.value), repr(e.value), e.value.reason, text, repr(store), str(store)):
        assert MARK not in t and "PRIVATE KEY" not in t and str(state) not in t
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert secret.splitlines()[1] not in text


def test_scrub_removes_key_blocks_and_long_base64():
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )
    out = lk.scrub(f"push failed\n{pem}\nafter {base64.b64encode(os.urandom(300)).decode()} end")
    assert "PRIVATE KEY" not in out and "push failed" in out and "after [redacted] end" in out
    assert lk.scrub(f"-----BEGIN OPENSSH PRIVATE KEY-----\n{MARK}") == "[redacted]"  # an unterminated block too


# -- pinned executables (Opus F2) --------------------------------------------------------------------------------------


@needs_system_git
def test_the_system_git_and_ssh_are_trusted():
    assert lk.pinned_exe("ssh") == "/usr/bin/ssh"


def test_a_user_owned_executable_is_not_trusted(tmp_path):
    exe = tmp_path / "ssh"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert lk.trusted_exe(str(exe)) is False


def test_a_user_writable_directory_chain_is_not_trusted(monkeypatch):
    # root-owned file, but a user-writable directory above it (the Homebrew case), simulated by lying about stat
    real_stat = os.stat

    def fake(path, *a, **k):
        st = real_stat(path, *a, **k)
        if str(path) == "/usr":
            return os.stat_result((st.st_mode | 0o002, *tuple(st)[1:]))
        return st

    monkeypatch.setattr(os, "stat", fake)
    assert lk.trusted_exe("/usr/bin/true") is False


def test_a_group_writable_executable_is_not_trusted(monkeypatch):
    # trusted_exe checks the RESOLVED file. Match on the realpath, not the name: on Linux /usr/bin/true can be a link
    # to a multi-call binary (uutils coreutils), whose path does not end in "/true".
    real_stat = os.stat
    target = os.path.realpath("/usr/bin/true")

    def fake(path, *a, **k):
        st = real_stat(path, *a, **k)
        if os.path.realpath(path) == target:
            return os.stat_result((st.st_mode | 0o020, *tuple(st)[1:]))
        return st

    monkeypatch.setattr(os, "stat", fake)
    assert lk.trusted_exe("/usr/bin/true") is False


def test_pinned_exe_refuses_an_untrusted_candidate_and_never_uses_path(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin")
    monkeypatch.setattr(lk, "EXE_CANDIDATES", {"ssh": (str(fake),)})
    with pytest.raises(LandKeyError) as e:
        lk.pinned_exe("ssh")
    assert e.value.code == "land.untrusted_exe"
    monkeypatch.setattr(lk, "EXE_CANDIDATES", {"ssh": ()})
    with pytest.raises(LandKeyError):
        lk.pinned_exe("ssh")  # nothing listed: PATH is never consulted
    with pytest.raises(LandKeyError):
        lk.pinned_exe("scp")


def test_the_candidates_are_system_directories():
    assert all(c.startswith("/usr/bin/") for cs in lk.EXE_CANDIDATES.values() for c in cs)


# -- the pin (never TOFU later) ----------------------------------------------------------------------------------------


def test_remote_host_forms():
    assert lk.remote_host("ssh://git@Example.COM/o/r.git") == "example.com"
    assert lk.remote_host("ssh://git@example.com:22/o/r") == "example.com"
    assert lk.remote_host("ssh://git@example.com:2222/o/r") == "[example.com]:2222"


@pytest.mark.parametrize("remote", [
    "git@example.com:o/r.git", "file:///tmp/x", "/tmp/x", "https://example.com/o/r", "ext::sh -c id", "ssh://example.com/r",
    "ssh://-oProxyCommand=x@h/r", "ssh://git@-oProxyCommand=x/r", "ssh://git@h/../r", "ssh://git@h/r r",
    "ssh://git@h/r\nx", "ssh://git:pw@h/r", "ssh://git@h:99999/r", "ssh://git@h", "ssh://git@[::1]/r",
])  # fmt: skip
def test_only_plain_ssh_remotes(remote):
    with pytest.raises(LandKeyError) as e:
        lk.remote_host(remote)
    assert e.value.code == "land.bad_remote"


def test_pin_writes_a_private_file_and_returns_fingerprints(store, state):
    line, b64 = host_line()
    fps = store.pin_host_key(f"ssh://git@{HOST}/o/r", "# a comment\n" + line)
    pin = next((state / "land").iterdir()) / "known_hosts"
    [f] = list(pin.iterdir())
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and f.read_text() == line
    kg = subprocess.run(["/usr/bin/ssh-keygen", "-lf", str(f)], capture_output=True, text=True, check=True)
    assert fps == [kg.stdout.split()[1]]


def test_pin_for_a_port_uses_the_bracket_form(store, state):
    line, _ = host_line("[example.com]:2222")
    store.pin_host_key("ssh://git@example.com:2222/o/r", line)
    with pytest.raises(LandKeyError):  # a line for the default port is not for this remote
        store.pin_host_key("ssh://git@example.com:2222/o/r", host_line()[0], replace=True)


@pytest.mark.parametrize("make", [
    lambda t: t.replace(HOST, "other.example"),  # another host
    lambda t: t.replace(HOST, "*.example.com"),  # wildcard
    lambda t: "@cert-authority " + t,  # marker
    lambda t: t.replace(HOST, "|1|c2FsdA==|aGFzaA=="),  # hashed
    lambda t: t.replace("ssh-ed25519", "ssh-dss"),  # not a plain key type
    lambda t: t.replace("ssh-ed25519", "ecdsa-sha2-nistp256"),  # type does not match the blob
    lambda t: t.split()[0] + " ssh-ed25519 !!!notbase64",
    lambda t: t.split()[0] + " ssh-ed25519",
    lambda t: "",
])  # fmt: skip
def test_pin_refuses_everything_that_is_not_a_plain_key_line_for_this_host(store, make):
    with pytest.raises(LandKeyError) as e:
        store.pin_host_key(f"ssh://git@{HOST}/o/r", make(host_line()[0]))
    assert e.value.code == "land.bad_host_key"


def test_pin_is_never_silently_replaced(store):
    r = f"ssh://git@{HOST}/o/r"
    store.pin_host_key(r, host_line()[0])
    with pytest.raises(LandKeyError) as e:
        store.pin_host_key(r, host_line()[0])
    assert e.value.code == "land.pin_exists"
    store.pin_host_key(r, host_line()[0], replace=True)


def test_pin_caps_the_number_of_lines(store):
    with pytest.raises(LandKeyError):
        store.pin_host_key(f"ssh://git@{HOST}/o/r", "".join(host_line()[0] for _ in range(9)))


@needs_system_git
def test_net_spec_needs_key_and_pin_and_never_trusts_on_first_use(store, state):
    r = f"ssh://git@{HOST}/o/r"
    with pytest.raises(LandKeyError) as e:
        store.net_spec(r)
    assert e.value.code == "land.no_key"
    store.generate()
    with pytest.raises(LandKeyError) as e:
        store.net_spec(r)
    assert e.value.code == "land.no_pin"
    store.pin_host_key(r, host_line()[0])
    spec = store.net_spec(r)
    assert spec.git == dev_git()[0] and spec.ssh == "/usr/bin/ssh" and spec.key.endswith("/key")
    # a pin for another remote does not serve this one
    with pytest.raises(LandKeyError) as e:
        store.net_spec("ssh://git@other.example/o/r")
    assert e.value.code == "land.no_pin"


@needs_system_git
def test_net_spec_refuses_a_loosened_pin(store, state):
    r = f"ssh://git@{HOST}/o/r"
    store.generate()
    store.pin_host_key(r, host_line()[0])
    [pin] = (next((state / "land").iterdir()) / "known_hosts").iterdir()
    pin.chmod(0o666)
    with pytest.raises(LandKeyError) as e:
        store.net_spec(r)
    assert e.value.code == "land.key_unsafe"


# -- the operations: command construction ------------------------------------------------------------------------------

OID = "a" * 40
REF = "refs/heads/main"
URL = "ssh://git@example.com/o/r.git"
SPEC = gitsafe.NetSpec(
    git="/usr/bin/git", ssh="/usr/bin/ssh", key="/h/land/ws/key", known_hosts="/h/land/ws/known_hosts/p"
)


def test_only_the_two_network_operations_have_a_network():
    assert {k for k, v in gitsafe.LAND_OPS.items() if v.net} == {"land_push", "land_fetch"}
    assert not any(v.net for v in gitsafe.OPS.values())


def test_ssh_command_is_built_by_us_with_every_fence():
    cmd = SPEC.ssh_command()
    assert cmd.startswith("/usr/bin/ssh ")
    for want in (
        "-i /h/land/ws/key", "-F /dev/null", "IdentitiesOnly=yes", "IdentityAgent=none",
        "UserKnownHostsFile=/h/land/ws/known_hosts/p", "GlobalKnownHostsFile=/dev/null", "StrictHostKeyChecking=yes",
        "UpdateHostKeys=no", "BatchMode=yes", "ProxyCommand=none", "ProxyJump=none", "ForwardAgent=no",
        "ControlPath=none", "PermitLocalCommand=no", "PasswordAuthentication=no",
    ):  # fmt: skip
        assert want in cmd, want


@pytest.mark.parametrize(
    "bad", ["/h/a b/key", "/h/a'b/key", "/h/$(id)/key", "/h/a;b", "rel/key", "/h/../key", "/h/a\nb"]
)
def test_paths_in_the_ssh_command_are_plain(bad):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.NetSpec(git="/usr/bin/git", ssh="/usr/bin/ssh", key=bad, known_hosts="/h/k")


@pytest.fixture
def bare(tmp_path):
    p = tmp_path.resolve() / "land.git"
    p.mkdir(mode=0o700)
    argv, env = gitsafe.land_init(str(p), {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    subprocess.run(argv, env=env, check=True, capture_output=True)
    return str(p)


OWN = {
    "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
    "HOME": "/nonexistent",
    "SSH_AUTH_SOCK": "/tmp/agent",
    "GIT_SSH": "/x",
}


def test_land_push_argv_has_no_force_and_a_lease_on_the_exact_value(bare):
    argv, env = gitsafe.command(bare, "land_push", OWN, operand=(REF, OID, URL, "b" * 40, REF), net=SPEC)
    assert argv[0] == "/usr/bin/git"
    i = argv.index("push")
    tail = argv[i:]
    assert tail == [
        "push", "--atomic", "--porcelain", "--no-verify", "--no-signed", "--no-recurse-submodules",
        f"--force-with-lease={REF}:{OID}", "--", URL, f"{'b' * 40}:{REF}",
    ]  # fmt: skip
    assert not any(
        a.startswith("+") or (a.startswith("--force") and not a.startswith("--force-with-lease=")) for a in tail
    )
    assert "-c" in argv and "protocol.ssh.allow=always" in argv and "protocol.allow=never" in argv


def test_land_fetch_argv_names_one_ref_and_one_local_name(bare):
    tref = gitsafe.target_tracking_ref(REF)
    argv, _ = gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, tref), net=SPEC)
    tail = argv[argv.index("fetch") :]
    assert tail[-3:] == ["--", URL, f"+{REF}:{tref}"] and "--no-tags" in tail and "--no-write-fetch-head" in tail
    assert tref.startswith("refs/land/target/") and len(tref) == len("refs/land/target/") + 64
    assert "transfer.fsckObjects=true" in argv and "fsck.hasDotgit=error" in argv


def test_network_environment_is_ssh_only_and_has_no_agent_or_caller_ssh(bare):
    _, env = gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, gitsafe.target_tracking_ref(REF)), net=SPEC)
    assert env["GIT_ALLOW_PROTOCOL"] == "ssh" and env["GIT_SSH_VARIANT"] == "ssh"
    assert env["GIT_SSH_COMMAND"] == SPEC.ssh_command() and env["PATH"] == "/usr/bin:/bin"
    assert "SSH_AUTH_SOCK" not in env and "GIT_SSH" not in env and "SSH_ASKPASS" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any("key" in k.lower() and k != "GIT_SSH_COMMAND" for k in env)


def test_a_network_op_needs_its_spec_and_no_other_op_gets_one(bare):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, gitsafe.target_tracking_ref(REF)))
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_has", OWN, operand=OID, net=SPEC)


@pytest.mark.parametrize("url", [
    "file:///tmp/x", "/tmp/x", "https://example.com/r", "git@example.com:o/r", "ssh://git@h/../r", "ssh://-oX=1@h/r",
    "ssh://git@h/r?x", "ssh://git@h//r", "ext::sh -c id", "ssh://git@h:0/r", "ssh://git@h:70000/r",
])  # fmt: skip
def test_the_url_operand_is_one_plain_ssh_url(bare, url):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(url, REF, gitsafe.target_tracking_ref(REF)), net=SPEC)


@pytest.mark.parametrize("ref", [
    "main", "refs/tags/v1", "refs/heads/", "refs/heads/../x", "refs/heads/-x", "refs/heads/a b", "refs/heads/x.lock",
    "refs/land/target/" + "a" * 64, "refs/heads/a\nb", "refs/heads/a:b",
])  # fmt: skip
def test_the_ref_operand_is_a_full_valid_branch_ref(bare, ref):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_push", OWN, operand=(ref, OID, URL, OID, "refs/heads/main"), net=SPEC)
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_push", OWN, operand=("refs/heads/main", OID, URL, OID, ref), net=SPEC)


def test_push_source_is_always_a_full_object_id(bare):
    for bad in ("", "HEAD", "main", "a" * 39, "A" * 40, "a" * 40 + "^"):
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(bare, "land_push", OWN, operand=(REF, OID, URL, bad, REF), net=SPEC)
        with pytest.raises(gitsafe.Unsafe):
            gitsafe.command(bare, "land_push", OWN, operand=(REF, bad, URL, OID, REF), net=SPEC)


def test_the_tracking_ref_cannot_be_chosen(bare):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, "refs/heads/main"), net=SPEC)
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, "refs/land/target/../../heads/main"), net=SPEC)


def test_the_gate_still_runs_before_a_network_op(bare):
    Path(bare, "config").write_text("[core]\n\tbare = true\n[core]\n\tsshCommand = /bin/evil\n")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, gitsafe.target_tracking_ref(REF)), net=SPEC)


# -- end to end through a fake ssh -------------------------------------------------------------------------------------


class Net:
    """A World plus a fake ssh, a store with a key and a pin, and the remote URL of its bare origin."""

    def __init__(self, base: Path, make_repo, state: Path, monkeypatch) -> None:  # noqa: ANN001
        self.w = World(base, make_repo)
        self.log = base / "ssh.log"
        self.fail = base / "ssh.fail"
        exec_path = dev_git()[1]
        script = base / "bin" / "ssh"
        script.parent.mkdir()
        script.write_text(
            textwrap.dedent(f"""\
            #!/bin/sh
            {{ printf 'ENV SSH_AUTH_SOCK=%s\\n' "${{SSH_AUTH_SOCK-unset}}"
              for a in "$@"; do printf 'ARG %s\\n' "$a"; done; }} >> '{self.log}'
            [ -e '{self.fail}' ] && exit 255
            while [ $# -gt 0 ]; do case "$1" in -i|-F|-o|-p|-l) shift 2;; -*) shift;; *) break;; esac; done
            shift
            PATH='{exec_path}':/usr/bin:/bin
            export PATH
            exec sh -c "$*"
        """)
        )  # noqa: E501
        script.chmod(0o755)
        self.fake = str(script)
        orig = lk.trusted_exe
        monkeypatch.setattr(lk, "EXE_CANDIDATES", {"ssh": (self.fake,)})
        monkeypatch.setattr(lk, "trusted_exe", lambda p: p == self.fake or orig(p))
        self.remote = f"ssh://git@{HOST}{self.w.origin}"
        self.store = KeyStore(state, "ws-net")
        self.store.generate()
        self.store.pin_host_key(self.remote, host_line()[0])
        self.key_path = next((state / "land").iterdir()) / "key"

    def calls(self) -> int:
        return self.log.read_text().count("ENV SSH_AUTH_SOCK") if self.log.exists() else 0

    def origin_main(self) -> str:
        return sh("-C", str(self.w.origin), "rev-parse", "refs/heads/main", env=self.w.env)


@pytest.fixture
def net(make_repo, tmp_path, state, monkeypatch):
    if not os.path.isfile(REAL_GIT):
        pytest.skip("no /usr/bin/git")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/should-not-leak")
    return Net(tmp_path.resolve(), make_repo, state, monkeypatch)


def land(net: Net, rel: str = "src/b.txt"):
    t0 = fetch_target(net.w.host, net.store, net.remote, "main")
    s = net.w.ticket(rel, "b\n")
    net.w.importing()
    return t0, s, net.w.cand(target=t0, source=s, head=t0)


def test_fetch_brings_only_the_target_into_a_local_tracking_ref(net):
    net.w.target.git("checkout", "-q", "-b", "other")
    net.w.target.write("o.txt", "o\n")
    net.w.target.commit("other")
    net.w.target.git("push", "-q", "origin", "other")
    got = fetch_target(net.w.host, net.store, net.remote, "main")
    assert got == net.origin_main()
    refs = net.w.hgit("for-each-ref", "--format=%(refname)").split()
    assert refs == [gitsafe.target_tracking_ref("refs/heads/main")]  # nothing else came over, not even "other"


def test_fetch_goes_through_our_ssh_command_only(net):
    fetch_target(net.w.host, net.store, net.remote, "main")
    log = net.log.read_text()
    assert "ENV SSH_AUTH_SOCK=unset" in log
    for want in ("ARG -i", f"ARG {net.key_path}", "ARG IdentitiesOnly=yes", "ARG StrictHostKeyChecking=yes",
                 "ARG IdentityAgent=none", "ARG -F", "ARG /dev/null"):  # fmt: skip
        assert want in log, want
    assert "ARG git@example.com" in log and "git-upload-pack" in log


def test_fetch_of_a_missing_branch_is_a_refusal(net):
    with pytest.raises(LandGitError) as e:
        fetch_target(net.w.host, net.store, net.remote, "nope")
    assert e.value.code == "land.remote_failed"


def test_fast_forward_push_moves_the_remote_and_is_verified(net):
    t0, s, cand = land(net)
    assert cand.kind == "fast_forward" and net.origin_main() == t0
    push_candidate(net.w.host, net.store, net.remote, "main", cand)
    assert net.origin_main() == cand.sha == s
    log = net.log.read_text()
    assert "git-receive-pack" in log and "ENV SSH_AUTH_SOCK=unset" in log


def test_merge_push_after_the_target_moved_on(net):
    t0 = fetch_target(net.w.host, net.store, net.remote, "main")
    s = net.w.ticket("src/b.txt", "b\n")
    t1 = net.w.advance_target("src/t.txt", "t\n")
    t1 = fetch_target(net.w.host, net.store, net.remote, "main")
    net.w.importing()
    cand = net.w.cand(target=t1, source=s, head=t0)
    assert cand.kind == "merge"
    push_candidate(net.w.host, net.store, net.remote, "main", cand)
    assert net.origin_main() == cand.sha


def test_a_target_that_moved_since_is_refused_and_nothing_moves(net):
    t0, s, cand = land(net)
    moved = net.w.advance_target("src/t.txt", "t\n")  # someone pushed between the fetch and our push
    with pytest.raises(LandGitError) as e:
        push_candidate(net.w.host, net.store, net.remote, "main", cand)
    assert e.value.code == "land.target_moved"
    assert net.origin_main() == moved


def test_a_remote_that_was_rewound_is_not_forced_back(net):
    t0, s, cand = land(net)
    net.w.target.write("src/t.txt", "t\n")
    t1 = net.w.target.commit("t")
    net.w.target.git("push", "-q", "origin", "main")
    net.w.rewind_target(t0)  # moved forward and back: same value again, the lease still holds exactly
    push_candidate(net.w.host, net.store, net.remote, "main", cand)
    assert net.origin_main() == cand.sha and t1 != t0


def test_a_candidate_that_does_not_extend_its_target_never_reaches_ssh(net):
    t0, s, cand = land(net)
    before = net.calls()
    other = net.w.ticket("src/z.txt", "z\n", "z")  # not a descendant of the target as recorded in the candidate
    net.w.importing()
    for bad in (
        Candidate(s, "fast_forward", other, s),  # target is *newer* than the candidate
        Candidate(cand.sha, "merge", t0, s) if cand.kind == "merge" else Candidate(s, "merge", t0, s),
        Candidate(s, "other-kind", t0, s),
        Candidate(s, "fast_forward", s, s),  # nothing to push
    ):  # fmt: skip
        with pytest.raises(LandGitError) as e:
            push_candidate(net.w.host, net.store, net.remote, "main", bad)
        assert e.value.code == "land.changed"
    assert net.calls() == before and net.origin_main() == t0


def test_no_pin_means_no_ssh_at_all(net, state):
    t0, s, cand = land(net)
    before = net.calls()
    other = "ssh://git@unpinned.example" + str(net.w.origin)
    for fn in (lambda: fetch_target(net.w.host, net.store, other, "main"),
               lambda: push_candidate(net.w.host, net.store, other, "main", cand)):  # fmt: skip
        with pytest.raises(LandGitError) as e:
            fn()
        assert e.value.code == "land.no_pin"
    assert net.calls() == before


def test_a_failing_ssh_is_a_refusal_with_no_ssh_text(net):
    t0, s, cand = land(net)
    net.fail.write_text("x")
    with pytest.raises(LandGitError) as e:
        push_candidate(net.w.host, net.store, net.remote, "main", cand)
    assert e.value.code == "land.remote_failed" and str(net.key_path) not in str(e.value)
    assert net.origin_main() == t0


def test_the_branch_name_cannot_smuggle_an_option_or_a_second_ref(net):
    for bad in ("main:other", "-f", "../x", "refs/heads/main", "a b", "x\ny"):
        with pytest.raises(LandGitError):
            fetch_target(net.w.host, net.store, net.remote, bad)
    assert net.calls() == 0


def test_the_key_text_is_not_in_the_environment_or_command_line_of_anything(net):
    land(net)
    secret_line = net.key_path.read_text().splitlines()[1]
    assert secret_line not in net.log.read_text()


# -- Codex on #762: named-remote redirect, PATH git, ACLs ------------------------------------------


def test_a_remote_named_like_the_url_cannot_redirect_a_network_operation(bare):
    """git resolves a URL operand that spells a configured remote's name through that remote."""
    cfg = Path(bare, "config")
    cfg.write_text(cfg.read_text() + f'[remote "{URL}"]\n\turl = ssh://git@evil.example/other\n')
    with pytest.raises(gitsafe.Unsafe, match="remote"):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, gitsafe.target_tracking_ref(REF)), net=SPEC)
    with pytest.raises(gitsafe.Unsafe, match="remote"):
        gitsafe.command(bare, "land_has", OWN, operand=OID)  # every landing operation, not only the network ones


def test_git_comes_from_the_pinned_path_never_from_path(net, tmp_path, monkeypatch):
    marker = tmp_path / "wrapper-ran"
    wrapper = tmp_path / "evil" / "git"
    wrapper.parent.mkdir()
    wrapper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec {dev_git()[0]} \"$@\"\n")
    wrapper.chmod(0o755)
    own = {**net.w.own, "PATH": f"{wrapper.parent}:/usr/bin:/bin"}
    repo = LandRepo(net.w.host_path, own)
    assert repo.git == dev_git()[0] and repo.own["PATH"] == "/usr/bin:/bin"
    t0 = fetch_target(repo, net.store, net.remote, "main")  # gate, ancestry reads and the fetch itself
    assert repo.has(t0) and repo.is_ancestor(t0, t0)
    assert not marker.exists()


def fake_developer_dir(base: Path) -> tuple[Path, Path]:
    """``<dev>/usr/bin/git`` + ``<dev>/usr/libexec/git-core`` and a link to ``<dev>``, all owned by the test user."""
    dev = base.resolve() / "Developer"
    (dev / "usr" / "bin").mkdir(parents=True)
    (dev / "usr" / "libexec" / "git-core").mkdir(parents=True)
    git = dev / "usr" / "bin" / "git"
    git.write_text("#!/bin/sh\n")
    git.chmod(0o755)
    link = base.resolve() / "xcode_select_link"
    link.symlink_to(dev)
    return dev, link


@pytest.fixture
def real_resolution(monkeypatch):
    monkeypatch.setattr(lk, "pinned_git", lk.resolve_git)


def test_macos_git_is_the_developer_dir_behind_the_root_owned_link_not_the_shim(real_resolution, tmp_path, monkeypatch):
    dev, link = fake_developer_dir(tmp_path)
    monkeypatch.setattr(lk, "PLATFORM", "darwin")
    monkeypatch.setattr(lk, "CLT_DIR", str(tmp_path / "no-clt"))
    monkeypatch.setattr(lk, "XCODE_LINK", str(link))
    # user-owned (as an ordinary Xcode install is): refused, with a plain hint
    with pytest.raises(LandKeyError) as e:
        lk.resolve_git()
    assert e.value.code == "land.untrusted_exe" and "Command Line Tools" in e.value.reason
    with pytest.raises(LandGitError) as e2:  # LandRepo refuses too: no landing git process starts
        LandRepo(str(tmp_path / "x.git"))
    assert e2.value.code == "land.untrusted_exe"
    # root-owned (the owner check is the test seam; tests cannot chown): accepted, and never /usr/bin/git
    monkeypatch.setattr(lk, "trusted_link", lambda p: True)
    monkeypatch.setattr(lk, "trusted_exe", lambda p: True)
    monkeypatch.setattr(lk, "_chain_ok", lambda p: True)
    assert lk.resolve_git() == (str(dev / "usr/bin/git"), str(dev / "usr/libexec/git-core"))


def test_macos_refuses_a_missing_or_foreign_link_or_helper_dir(real_resolution, tmp_path, monkeypatch):
    dev, link = fake_developer_dir(tmp_path)
    monkeypatch.setattr(lk, "PLATFORM", "darwin")
    monkeypatch.setattr(lk, "CLT_DIR", str(tmp_path / "no-clt"))
    monkeypatch.setattr(lk, "trusted_exe", lambda p: True)
    monkeypatch.setattr(lk, "_chain_ok", lambda p: True)
    monkeypatch.setattr(lk, "XCODE_LINK", str(tmp_path / "nope"))
    monkeypatch.setattr(lk, "trusted_link", lambda p: True)
    with pytest.raises(LandKeyError):  # no link
        lk.resolve_git()
    monkeypatch.setattr(lk, "XCODE_LINK", str(link))
    monkeypatch.setattr(lk, "trusted_link", lambda p: False)
    with pytest.raises(LandKeyError):  # a link that is not root-owned
        lk.resolve_git()
    monkeypatch.setattr(lk, "trusted_link", lambda p: True)
    shutil.rmtree(dev / "usr" / "libexec" / "git-core")
    with pytest.raises(LandKeyError):  # no helper directory
        lk.resolve_git()


def fake_clt(base: Path) -> Path:
    clt = base.resolve() / "CLT"
    (clt / "usr" / "bin").mkdir(parents=True)
    (clt / "usr" / "libexec" / "git-core").mkdir(parents=True)
    git = clt / "usr" / "bin" / "git"
    git.write_text("#!/bin/sh\n")
    git.chmod(0o755)
    return clt


def test_macos_prefers_the_command_line_tools_over_a_user_owned_xcode(real_resolution, tmp_path, monkeypatch):
    dev, link = fake_developer_dir(tmp_path)
    clt = fake_clt(tmp_path)
    monkeypatch.setattr(lk, "PLATFORM", "darwin")
    monkeypatch.setattr(lk, "XCODE_LINK", str(link))
    monkeypatch.setattr(lk, "CLT_DIR", str(clt))
    monkeypatch.setattr(lk, "trusted_link", lambda p: True)
    monkeypatch.setattr(lk, "_chain_ok", lambda p: True)
    # the Command Line Tools are trusted, the xcode-select dir (user-owned) is not
    monkeypatch.setattr(lk, "trusted_exe", lambda p: str(p).startswith(str(clt)))
    assert lk.resolve_git() == (str(clt / "usr/bin/git"), str(clt / "usr/libexec/git-core"))
    # CLT untrusted: fall back to the xcode_select_link dir when that is trusted
    monkeypatch.setattr(lk, "trusted_exe", lambda p: str(p).startswith(str(dev)))
    assert lk.resolve_git() == (str(dev / "usr/bin/git"), str(dev / "usr/libexec/git-core"))
    # CLT missing: same fallback
    monkeypatch.setattr(lk, "CLT_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(lk, "trusted_exe", lambda p: True)
    assert lk.resolve_git()[0] == str(dev / "usr/bin/git")
    # both untrusted: refused
    monkeypatch.setattr(lk, "CLT_DIR", str(clt))
    monkeypatch.setattr(lk, "trusted_exe", lambda p: False)
    with pytest.raises(LandKeyError) as e:
        lk.resolve_git()
    assert e.value.code == "land.untrusted_exe"


def test_the_real_trusted_link_check_needs_a_root_owned_link(tmp_path):
    _dev, link = fake_developer_dir(tmp_path)
    assert lk.trusted_link(str(link)) is False and lk.trusted_link(str(tmp_path / "missing")) is False


def test_linux_git_needs_a_root_owned_exe_and_exec_dir(real_resolution, monkeypatch):
    monkeypatch.setattr(lk, "PLATFORM", "linux")
    if not os.path.isfile("/usr/bin/git"):
        pytest.skip("no /usr/bin/git")
    monkeypatch.setattr(lk, "trusted_exe", lambda p: False)
    with pytest.raises(LandKeyError) as e:
        lk.resolve_git()
    assert e.value.code == "land.untrusted_exe"
    monkeypatch.setattr(lk, "trusted_exe", lambda p: True)
    monkeypatch.setattr(lk, "_chain_ok", lambda p: False)  # the exec dir chain is not root-owned
    with pytest.raises(LandKeyError):
        lk.resolve_git()
    monkeypatch.setattr(lk, "_chain_ok", lambda p: True)
    git, exec_dir = lk.resolve_git()
    want = subprocess.run(["/usr/bin/git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    assert git == "/usr/bin/git" and exec_dir == os.path.realpath(want)


def test_every_landing_git_process_runs_the_pinned_git_with_its_exec_path(net, tmp_path, monkeypatch):
    """The gate's own reads (version, config, rev-parse) are landing git processes too (Opus M1 on #762)."""
    git, exec_dir = dev_git()
    log = tmp_path / "git.log"
    wrapper = tmp_path / "pin" / "git"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\necho "$1 $GIT_EXEC_PATH" >> \'{log}\'\nexec {git} "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(lk, "pinned_git", lambda: (str(wrapper), exec_dir))
    monkeypatch.setattr(gitsafe, "_VERSIONS", {})
    repo = LandRepo(net.w.host_path, {"PATH": f"{tmp_path}/evil:/usr/bin:/bin", "HOME": str(tmp_path)})
    repo.has("a" * 40)
    lines = log.read_text().splitlines()
    assert any(ln.startswith("--version") for ln in lines)  # the gate's version read
    assert len(lines) >= 4 and all(ln.endswith(f" {exec_dir}") or ln.startswith("-c ") for ln in lines)
    assert all(exec_dir in ln for ln in lines)


def test_public_key_reads_the_descriptor_it_checked_not_the_path(store, monkeypatch):
    store.generate()

    def no_open(*a, **k):
        raise AssertionError("reopened by path")

    monkeypatch.setattr("builtins.open", no_open)
    assert store.public_key().startswith("ssh-ed25519 ")


def test_a_command_with_a_git_path_must_be_a_landing_op_with_a_plain_path(bare):
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_has", OWN, operand=OID, git="/h/a b/git")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "staged_raw", OWN, git="/usr/bin/git")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.command(bare, "land_fetch", OWN, operand=(URL, REF, gitsafe.target_tracking_ref(REF)), net=SPEC,
                        git="/usr/bin/other")  # fmt: skip
    argv, _ = gitsafe.command(bare, "land_has", OWN, operand=OID, git="/usr/bin/git")
    assert argv[0] == "/usr/bin/git"


darwin = pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs (chmod +a)")


@darwin
def test_an_acl_on_the_key_file_or_a_directory_is_refused(store, state):
    store.generate()
    ws = next((state / "land").iterdir())
    for target in (ws / "key", ws):
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(target)], check=True)
        with pytest.raises(LandKeyError) as e:
            store.public_key()
        assert e.value.code == "land.key_unsafe"
        subprocess.run(["/bin/chmod", "-N", str(target)], check=True)
    store.public_key()  # clean again


@darwin
def test_a_write_acl_on_an_executable_makes_it_untrusted(tmp_path):
    exe = tmp_path / "x"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert lk._acl_ok(exe, {0}, empty=False) is True
    subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(exe)], check=True)
    assert lk._acl_ok(exe, {0}, empty=False) is False
