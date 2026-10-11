"""F1 vectors that the store implements: ``body.md`` section text (§4), artifact references (§5.8) and the repo
identity read from a raw remote (§5.7). The vector files are written by the independent oracle."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from orch import canon
from orch.store import observe, render

DIR = Path(__file__).parent.parent / "vectors" / "f1"


def load(name):
    return json.loads((DIR / name).read_text(encoding="utf-8"))


ST = load("section_text.json")["cases"]


@pytest.mark.parametrize("c", ST, ids=lambda c: c["name"])
def test_section_text(c):
    if "refused" in c:
        with pytest.raises(render.BodyError):
            render.parse_body(c["body"], c["type"])
        return
    secs = render.parse_body(c["body"], c["type"])
    assert secs == c["sections"]
    for sid, text in secs.items():
        assert canon.section_hash(text) == c["hashes"][sid]  # what is hashed is the trimmed text, nothing else


AR = load("artifact_refs.json")


def test_the_regex_is_the_spec_regex():
    assert render._REFS.pattern == AR["regex"]


@pytest.mark.parametrize("c", AR["cases"], ids=lambda c: c["name"])
def test_artifact_refs(c):
    assert render.refs_of(c["text"]) == c["refs"]
    assert render.section_entry(c["text"])["refs"] == c["refs"]


def test_signer_refs_that_differ_from_the_regex_are_refused(env):
    from orch.store import StoreError

    s = env.bootstrap()
    uid = env.new_ticket()
    text = "see (artifact:a.png) and (artifact:b.png)"
    for refs in ([], ["a.png"], ["a.png", "b.png", "c.png"], ["b.png", "a.png"]):
        e = {
            "type": "ticket.updated",
            "actor": env.agent,
            "base_rev": env.base_rev(uid, {}, {"context": None}),
            "sections": {"context": {"hash": canon.section_hash(text), "refs": refs}},
        }
        with pytest.raises(StoreError) as ei:
            s.append(e, log=uid, body={"context": text})
        assert ei.value.code == "body.bad_refs", refs


RM = load("repo_identity.json")["mapping"]


# mapped results that are not canonical, which F1 5.7 refuses; orch converts them instead (PR #361 fix round)
REFUSED_GAPS = {
    "https_upper_dot_git_refused": "orch strips an upper-case .GIT; F1 removes one trailing .git, then refuses .GIT",
    "https_double_trailing_slash_refused": "orch strips both slashes; F1 removes one, then refuses the trailing slash",
    "ssh_port_443_refused": "orch turns the port into a path segment; F1 keeps 443 for ssh, then refuses it",
}


def _mapping_param(c):
    if c["name"] in KNOWN_GAPS:
        return pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="repo_identity deviates from F1 5.7"))
    if c["name"] == "scp_like_with_a_local_insteadof":
        return pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="orch reads `git remote get-url`, which "
                            "applies the repo-local insteadOf (https://evil.example/acme/x); F1 5.7 maps the raw "
                            "remote.origin.url. Fixed in #363"))  # fmt: skip
    if c["name"] in REFUSED_GAPS:
        return pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=REFUSED_GAPS[c["name"]]))
    return c


@pytest.mark.parametrize(
    "c",
    RM,
    ids=[c["name"] for c in RM],
)
def test_repo_identity_from_a_raw_remote(tmp_path, monkeypatch, c):
    # the developer's ~/.gitconfig (insteadOf, ...) must not reach the mapping: git here and inside orch sees no
    # global or system config
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if c["raw"]:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", c["raw"]], check=True)
    for k, v in c.get("local_git_config", {}).items():
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    if c.get("refused"):  # §5.7: a mapped result that is not canonical is refused, never converted to a https identity
        try:
            got = observe.repo_identity(tmp_path, c["repo_name"])
        except Exception:  # noqa: BLE001 - refusing by raising is one valid outcome (F1 leaves the form open)
            return
        assert not got.startswith("https://"), got
        return
    got = observe.repo_identity(tmp_path, c["repo_name"])
    assert got == c["canonical"]
    assert "s3cr3t" not in got and "ghp_" not in got  # a token never lands in a hashed value
    if not got.startswith("local:"):
        assert canon.check_repo_identity(got) == got


def test_a_token_in_the_remote_is_not_in_the_result_or_the_canonical_form():
    for c in RM:
        assert "s3cr3t" not in (c["canonical"] or "") and "ghp_" not in (c["canonical"] or "")
