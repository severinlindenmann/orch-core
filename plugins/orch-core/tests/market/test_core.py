"""V3 G2: the pure parts of ``orch.market`` against the G0 vectors, and the closed git operations of ``orch.gitsafe``
(argv and environment exactly; a real git over a local repository for the reading half)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orch import gitsafe
from orch.addons.package import package_digest
from orch.market import MarketError, address, constants, fetch, index, policy
from orch.market.url import canonical_market_url
from tests.market import helpers as H
from tests.tamper import tamper_b64u

VECTORS = json.loads((Path(__file__).parents[1] / "addons/vectors_market.json").read_text())["cases"]


def code_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except MarketError as exc:
        return exc.code
    return None


@pytest.mark.parametrize("case", [c for c in VECTORS if c["check"] == "index"], ids=lambda c: c["name"])
def test_index_vectors_give_the_reference_answers(case):
    arg = case["args"]["index"]
    raw = arg.encode() if isinstance(arg, str) else json.dumps(arg).encode()
    got = code_of(lambda: index.check(index.parse(raw)))
    assert got == case["expect"].get("refuse")


@pytest.mark.parametrize("case", [c for c in VECTORS if c["check"] == "address"], ids=lambda c: c["name"])
def test_address_vectors_give_the_reference_answers(case):
    assert code_of(address.check_address, case["args"]["ip"]) == case["expect"].get("refuse")


KNOWN_DIGEST = "sha256:3f8e89e323846e8e54e539550c34b536d78eb959ee4db7d925211a4931d7e897"


def test_the_package_digest_has_a_fixed_known_answer():
    """The same bytes and the same answer as in orch-marketplace's tests (``tests/test_digest.py``): the two
    implementations cannot drift apart without one of the two suites failing in CI."""
    assert package_digest({"orch-addon.json": b"{}", "a/b.txt": b"x"}) == KNOWN_DIGEST


def test_the_digest_is_the_one_core_computes_and_the_marketplace_validator_agrees():
    files = {"orch-addon.json": b"{}", "a/b.txt": b"x"}
    want = package_digest(files)
    here = Path(__file__).resolve()
    found = [p / "orch-marketplace/scripts/validate_index.py" for p in (here.parents[5], here.parents[6])]
    validator = next((v for v in found if v.exists()), None)
    if validator is None:
        pytest.skip("the orch-marketplace clone is not next to this checkout")
    import importlib.util

    spec = importlib.util.spec_from_file_location("validate_index", validator)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.package_digest(files) == want


# ------------------------------------------------------------------------------------------------ url, gates


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://GitHub.com/a/b",
        "https://github.com/A/b",
        "https://github.com/a/b/",
        "https://github.com/a/b.git",
        "https://github.com:443/a/b",
        "https://u@github.com/a/b",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b#x",
        "https://127.0.0.1/a",
        "https://localhost/a",
        "https://github.com/a/../b",
        "https://github.com/",
        "https://github.com",
        "https://bücher.de/a",
        5,
        None,
    ],
)
def test_canonical_market_url_rejects_and_never_repairs(url):
    assert code_of(canonical_market_url, url) == "market.malformed"


def test_canonical_market_url_accepts_the_canonical_form():
    assert canonical_market_url(constants.OFFICIAL_URL) == constants.OFFICIAL_URL
    assert canonical_market_url("https://git.example.org/Acme/Market") == "https://git.example.org/Acme/Market"


def test_E8_the_class_and_hub_gate():
    assert policy.gate(constants.OFFICIAL_URL, hub_joined=False) == "official"
    assert code_of(policy.gate, "https://github.com/acme/m", hub_joined=False) == "market.community_unisolated"
    assert code_of(policy.gate, "https://github.com/acme/m", hub_joined=True) == "market.not_allowed"
    assert (
        code_of(policy.gate, constants.OFFICIAL_URL, hub_joined=True) == "market.not_allowed"
    )  # policy unreadable: nothing
    assert code_of(policy.gate, "http://github.com/acme/m", hub_joined=True) == "market.malformed"  # canonical first


# ------------------------------------------------------------------------------------------------ the signature


def test_the_official_index_needs_expiry_and_a_signature_by_a_pinned_key(monkeypatch):
    doc = H.index(3)
    raw = H.raw(doc)
    now = 1_000.0
    # today the key list is empty: even a perfectly signed index is refused
    assert code_of(index.evaluate, raw, H.OFFICIAL, official=True, now=now) == "market.index_unsigned"
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64,))
    v = index.evaluate(raw, H.OFFICIAL, official=True, now=now)
    assert v.verified and not v.stale and v.doc["version"] == 3
    assert index.evaluate(raw, H.OFFICIAL, official=True, now=5_000_000_000).stale  # stale is a flag, not a refusal
    assert (
        code_of(index.evaluate, H.raw(H.index(3, signed=False)), H.OFFICIAL, official=True, now=now)
        == "market.index_unsigned"
    )
    no_expiry = H.index(3, signed=False)
    del no_expiry["expires_at"]
    H.ref.sign_index(no_expiry, H.KEY, H.KEY_B64)
    assert code_of(index.evaluate, H.raw(no_expiry), H.OFFICIAL, official=True, now=now) == "market.index_unsigned"
    other = H.ec.generate_private_key(H.ec.SECP256R1())
    assert (
        code_of(index.evaluate, H.raw(H.index(3, key=other)), H.OFFICIAL, official=True, now=now)
        == "market.index_unsigned"
    )
    tampered = json.loads(raw)
    tampered["version"] = 4  # covered by the signature
    assert (
        code_of(index.evaluate, json.dumps(tampered).encode(), H.OFFICIAL, official=True, now=now)
        == "market.index_unsigned"
    )
    tampered = json.loads(raw)
    tampered["title"] = "changed"  # display text is not covered
    assert index.evaluate(json.dumps(tampered).encode(), H.OFFICIAL, official=True, now=now).verified


def test_source_mismatch_is_judged_before_the_signature():
    assert code_of(
        index.evaluate, H.raw(H.index(1, url="https://github.com/x/y")), H.OFFICIAL, official=True, now=1
    ) == ("market.source_mismatch")


def test_E2_only_a_verified_index_has_a_floor():
    v_signed = type("V", (), {"verified": True, "doc": {"version": 3}, "payload_sha256": "p"})()
    v_unsigned = type("V", (), {"verified": False, "doc": {"version": 1}, "payload_sha256": "q"})()
    assert code_of(index.check_floor, 5, "p", v_signed) == "market.rollback"
    assert code_of(index.check_floor, 3, "other", v_signed) == "market.rollback"  # same version, other payload
    assert code_of(index.check_floor, 3, "p", v_signed) is None
    assert code_of(index.check_floor, None, None, v_signed) is None
    assert code_of(index.check_floor, 5, "p", v_unsigned) is None  # unverified: no floor to compare, none recorded


def test_a_present_signature_on_another_index_must_verify():
    other = "https://git.example.org/acme/market"
    doc = H.index(1, url=other)
    assert index.verify(doc, other, official=False) is True
    doc["signature"]["sig"] = tamper_b64u(doc["signature"]["sig"])
    assert code_of(index.verify, doc, other, official=False) == "market.index_unsigned"
    unsigned = H.index(1, url=other, signed=False)
    assert index.verify(unsigned, other, official=False) is False


def test_parse_is_strict_and_capped():
    assert code_of(index.parse, b'{"a": 1, "a": 2}') == "market.malformed"
    assert code_of(index.parse, b'{"a": 1.5}') == "market.malformed"
    assert code_of(index.parse, b"[]") == "market.malformed"
    assert code_of(index.parse, b"x" * (constants.MAX_INDEX_BYTES + 1)) == "market.malformed"
    assert code_of(index.parse, b'\xef\xbb\xbf{"a": 1}') == "market.malformed"  # a BOM


# ------------------------------------------------------------------------------------------------ addresses


def test_E7a_every_resolved_address_must_pass_and_none_is_picked(monkeypatch):
    def gai(host, port, type=0):  # noqa: A002
        return [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(address.socket, "getaddrinfo", gai)
    assert code_of(address.resolve, "github.com", 5) == "market.address_refused"
    monkeypatch.setattr(address.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert address.resolve("github.com", 5) == ["93.184.216.34"]
    monkeypatch.setattr(address.socket, "getaddrinfo", lambda *a, **k: [])
    assert code_of(address.resolve, "github.com", 5) == "market.fetch_failed"


def test_E7a_the_lookup_runs_under_a_deadline(monkeypatch):
    import time

    monkeypatch.setattr(address.socket, "getaddrinfo", lambda *a, **k: time.sleep(2) or [])
    t0 = time.monotonic()
    assert code_of(address.resolve, "github.com", 0.2) == "market.fetch_failed"
    assert time.monotonic() - t0 < 1.5


# ------------------------------------------------------------------------------------------------ gitsafe: argv and env


def test_E7_the_fetch_argv_is_exactly_the_closed_set():
    argv = gitsafe.market_fetch_argv(H.OFFICIAL, None, "github.com", "93.184.216.34", "/tmp/stg")
    assert argv[1:3] == ["-C", "/tmp/stg"]
    cs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    for want in (
        "protocol.allow=never", "protocol.https.allow=always", "http.followRedirects=false", "http.proxy=",
        "http.sslVerify=true", "transfer.fsckObjects=true", "fetch.fsckObjects=true",
        "core.hooksPath=/dev/null", "core.symlinks=false", "submodule.recurse=false", "credential.helper=",
        "http.curloptResolve=github.com:443:93.184.216.34",
    ):  # fmt: skip
        assert want in cs
    assert not [a for a in argv if a.lower().startswith(("http.sslcainfo", "http.sslcapath"))]  # curl keeps its CAs
    assert argv[-9:] == [
        "fetch",
        "--depth",
        "1",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        "--",
        H.OFFICIAL,
        "HEAD",
    ]
    pinned = gitsafe.market_fetch_argv(H.OFFICIAL, "a" * 64, "github.com", "2606:4700:4700::1111", "/tmp/stg")
    assert "http.curloptResolve=github.com:443:[2606:4700:4700::1111]" in pinned and pinned[-1] == "a" * 64


@pytest.mark.parametrize(
    "bad",
    [
        {"url": "http://github.com/a/b"}, {"url": "https://github.com/a/b?x=1"}, {"url": "https://other.com/a/b"},
        {"url": "https://github.com/a/b --upload-pack=x"}, {"commit": "main"}, {"commit": "abc"}, {"commit": "A" * 40},
        {"address": "github.com"}, {"address": "1.2.3.4 -x"}, {"address": "::"[:0] + "zz"}, {"host": "evil.com"},
    ],
)  # fmt: skip
def test_E7_the_fetch_argv_refuses_any_operand_that_could_be_an_option_or_another_place(bad):
    args = {"url": H.OFFICIAL, "commit": None, "host": "github.com", "address": "93.184.216.34"} | bad
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.market_fetch_argv(args["url"], args["commit"], args["host"], args["address"], "/tmp/stg")


def test_E7d_the_environment_is_built_from_scratch(monkeypatch, tmp_path):
    for k, v in {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/x", "GIT_DIR": "/x",
        "HTTPS_PROXY": "http://evil", "https_proxy": "http://evil", "ALL_PROXY": "x", "NO_PROXY": "x",
        "SSL_CERT_FILE": "/x", "CURL_CA_BUNDLE": "/x", "GIT_SSH_COMMAND": "evil", "GIT_ASKPASS": "evil",
        "SSH_ASKPASS": "evil", "GIT_EXEC_PATH": "/x", "ORCH_GRANT": "g", "GITHUB_TOKEN": "t",
        "GIT_ALLOW_PROTOCOL": "file",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    env = gitsafe._market_env(str(tmp_path))
    assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"] == "0"
    assert env["HOME"] == str(tmp_path) and env["GIT_NO_LAZY_FETCH"] == "1"
    banned = ("PROXY", "proxy", "SSL_CERT", "CURL_CA", "ASKPASS", "SSH", "ORCH_", "TOKEN", "GIT_DIR", "EXEC_PATH")
    assert not [k for k in env if any(b in k for b in banned)]
    assert not [k for k in env if k.startswith("GIT_CONFIG_KEY") or k.startswith("GIT_CONFIG_VALUE")]


def test_E7e_the_marketplace_fetch_is_a_separate_https_operation_and_existing_ops_stay_non_https():
    spec = gitsafe.NetSpec(git="/usr/bin/git", ssh="/usr/bin/ssh", key="/k/key", known_hosts="/k/hosts")
    assert gitsafe._net_env(spec)["GIT_ALLOW_PROTOCOL"] == "ssh"  # not widened
    assert "protocol.allow=never" in gitsafe.NEUTRAL and "protocol.https.allow=always" not in gitsafe.NEUTRAL
    assert gitsafe._market_env("/tmp")["GIT_ALLOW_PROTOCOL"] == "https"
    for op in list(gitsafe.OPS) + list(gitsafe.LAND_OPS):
        assert "https" not in " ".join(map(str, getattr(gitsafe.OPS.get(op) or gitsafe.LAND_OPS[op], "args", ())))


def test_a_redirect_is_address_refused_not_fetch_failed(monkeypatch):
    def run(*a, **k):
        raise gitsafe.MarketGitError("redirect")

    monkeypatch.setattr(address, "resolve", lambda h, t: ["93.184.216.34"])
    monkeypatch.setattr(gitsafe, "market_fetch", run)
    assert code_of(fetch.fetch_index, H.OFFICIAL, None) == "market.address_refused"
    for kind, want in (
        ("mismatch", "market.commit_mismatch"),
        ("too_big", "market.fetch_failed"),
        ("failed", "market.fetch_failed"),
    ):
        monkeypatch.setattr(
            gitsafe, "market_fetch", lambda *a, _k=kind, **k: (_ for _ in ()).throw(gitsafe.MarketGitError(_k))
        )
        assert code_of(fetch.fetch_index, H.OFFICIAL, None) == want


def test_a_git_that_reports_a_redirect_is_recognised(tmp_path):
    script = "echo 'warning: redirecting to https://127.0.0.1/' >&2; exit 128"
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe._market_run(["sh", "-c", script], str(tmp_path), str(tmp_path), timeout=5, cap=1 << 20)
    assert e.value.kind == "redirect"


# ------------------------------------------------------------------------------------------------ real git, local repo


def _git(*args, cwd, env=None):
    e = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        **(env or {}),
    }
    return subprocess.run(["git", *args], cwd=cwd, env=e, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603, S607


def make_repo(tmp_path, files: dict[str, bytes], links: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = tmp_path / "remote"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    for name, data in files.items():
        (repo / name).write_bytes(data)
    for name, target in (links or {}).items():
        (repo / name).symlink_to(target)
    _git("add", "-A", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def local(monkeypatch):
    """The production argv and environment, with the transport swapped for a local repository (the fixture changes
    only the URL and the allowed protocol: the settings and the limits under test are the production ones)."""
    holder: dict[str, str] = {}
    real_argv = gitsafe.market_fetch_argv

    def argv(url, commit, host, addr, staging):
        out = real_argv(url, commit, host, addr, staging)
        i = out.index("protocol.allow=never")
        del out[i - 1 : i + 1]  # the pair "-c protocol.allow=never": the local transport needs "file"
        out[out.index("--") + 1] = "file://" + holder["repo"]
        return [out[0], "-c", "protocol.file.allow=always", *out[1:]]

    real_env = gitsafe._market_env

    def env(home):
        return real_env(home) | {"GIT_ALLOW_PROTOCOL": "file"}

    monkeypatch.setattr(gitsafe, "market_fetch_argv", argv)
    monkeypatch.setattr(gitsafe, "_market_env", env)
    monkeypatch.setattr(address, "resolve", lambda h, t: ["93.184.216.34"])
    return holder


def test_fetch_reads_only_the_index_blob_of_the_commit_it_fetched(local, tmp_path, monkeypatch):
    repo, head = make_repo(tmp_path, {"marketplace.json": b'{"a": 1}', "other.txt": b"x"})
    local["repo"] = str(repo)
    monkeypatch.setenv("GIT_DIR", "/nonexistent")  # the caller's environment changes nothing
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/x")
    got, data = fetch.fetch_index(H.OFFICIAL, None)
    assert (got, data) == (head, b'{"a": 1}')
    assert fetch.fetch_index(H.OFFICIAL, head) == (head, b'{"a": 1}')
    assert (
        code_of(fetch.fetch_index, H.OFFICIAL, "c" * 40) == "market.fetch_failed"
    )  # no silent fallback to a branch head


def test_a_symlink_or_missing_index_or_oversized_blob_is_refused(local, tmp_path):
    local["repo"] = str(make_repo(tmp_path, {"x": b"1"}, {"marketplace.json": "/etc/passwd"})[0])
    assert code_of(fetch.fetch_index, H.OFFICIAL, None) == "market.fetch_failed"
    other = tmp_path / "b"
    other.mkdir()
    local["repo"] = str(make_repo(other, {"readme": b"1"})[0])
    assert code_of(fetch.fetch_index, H.OFFICIAL, None) == "market.fetch_failed"
    third = tmp_path / "c"
    third.mkdir()
    local["repo"] = str(make_repo(third, {"marketplace.json": b" " * (constants.MAX_INDEX_BYTES + 1)})[0])
    assert code_of(fetch.fetch_index, H.OFFICIAL, None) == "market.fetch_failed"


def test_E7b_the_byte_cap_is_a_mechanism(local, tmp_path, monkeypatch):
    blob = os.urandom(900_000)  # incompressible: the pack is about as big
    local["repo"] = str(make_repo(tmp_path, {"marketplace.json": b"{}", "big.bin": blob})[0])
    monkeypatch.setattr(gitsafe, "MARKET_MAX_PACK", 200_000)
    assert code_of(fetch.fetch_index, H.OFFICIAL, None) == "market.fetch_failed"


def test_E7b_the_wall_clock_kills_the_process_group(tmp_path):
    import time

    t0 = time.monotonic()
    with pytest.raises(gitsafe.MarketGitError):
        gitsafe._market_run(["sh", "-c", "sleep 30 & sleep 30"], str(tmp_path), str(tmp_path), timeout=0.5, cap=1 << 20)
    assert time.monotonic() - t0 < 5


def test_E7c_a_large_tree_is_never_listed(local, tmp_path, monkeypatch):
    files = {"marketplace.json": b"{}"} | {f"f{i:04d}": b"x" for i in range(1500)}
    local["repo"] = str(make_repo(tmp_path, files)[0])
    calls = []
    real = gitsafe._market_run

    def spy(argv, *a, **k):
        calls.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(gitsafe, "_market_run", spy)
    assert fetch.fetch_index(H.OFFICIAL, None)[1] == b"{}"
    ls = next(c for c in calls if "ls-tree" in c)
    assert "-r" not in ls and ls[-2:] == ["--", "marketplace.json"]


def test_hostile_content_of_the_remote_runs_nothing(local, tmp_path):
    marker = tmp_path / "ran"
    repo, _ = make_repo(
        tmp_path,
        {
            "marketplace.json": b"{}",
            ".gitattributes": b"* filter=evil\nmarketplace.json export-subst export-ignore\n",
            ".gitmodules": b'[submodule "x"]\n path=x\n url=file:///etc\n',
            ".git-hook": b"x",
        },
    )
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    _git("config", "filter.evil.smudge", f"touch {marker}", cwd=repo)
    local["repo"] = str(repo)
    assert fetch.fetch_index(H.OFFICIAL, None)[1] == b"{}"
    assert not marker.exists()


# ---- limits (no preexec_fn)


def test_the_file_size_limit_is_set_by_a_trampoline_not_by_preexec_fn(tmp_path):
    import inspect

    src = inspect.getsource(gitsafe._market_run)
    assert "preexec_fn" not in src.replace("``preexec_fn``", "")
    out = tmp_path / "elsewhere"
    out.mkdir()
    # the write goes outside the staging directory, where the watchdog does not look: only RLIMIT_FSIZE stops it
    with pytest.raises(gitsafe.MarketGitError):
        gitsafe._market_run(
            ["sh", "-c", f"head -c 3000000 /dev/zero > {out}/f"],
            str(tmp_path),
            str(tmp_path / "stg"),
            timeout=10,
            cap=100_000,
        )
    assert out.joinpath("f").stat().st_size <= 100_000


def test_output_is_read_with_a_cap_and_a_chatty_command_is_killed(tmp_path):
    import time

    t0 = time.monotonic()
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe._market_run(
            ["sh", "-c", "yes"], str(tmp_path), str(tmp_path), timeout=20, cap=1 << 30, stdout_cap=100_000
        )
    assert e.value.kind == "too_big" and time.monotonic() - t0 < 10
    with pytest.raises(gitsafe.MarketGitError) as e:
        gitsafe._market_run(["sh", "-c", "yes >&2"], str(tmp_path), str(tmp_path), timeout=20, cap=1 << 30)
    assert e.value.kind == "too_big"
    assert (
        gitsafe._market_run(["sh", "-c", "printf abc"], str(tmp_path), str(tmp_path), timeout=5, cap=1 << 20) == b"abc"
    )


#: a commit of orch-marketplace's develop that holds the (empty) index; main has none until develop is merged
INDEX_COMMIT = "3e73e374c5dc78e223bd1ef9b1f6351cfc18973f"


@pytest.mark.slow
def test_a_real_https_fetch_of_the_official_index_reaches_the_signature_check():
    """L2-G2i-1: with curl's own CA store the fetch works; today the index is unsigned, so the answer is
    ``market.index_unsigned`` (fail closed), never ``market.fetch_failed``. Needs the network and github.com."""
    try:
        got, data = fetch.fetch_index(constants.OFFICIAL_URL, INDEX_COMMIT)
    except MarketError as exc:
        if exc.code == "market.address_refused":
            pytest.skip("no usable network")
        raise
    assert got == INDEX_COMMIT and data
    assert code_of(index.evaluate, data, constants.OFFICIAL_URL, official=True, now=1.0) == "market.index_unsigned"
