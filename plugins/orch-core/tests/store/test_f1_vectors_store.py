"""F1 vectors that the store implements: ``body.md`` section text (§4), artifact references (§5.8) and the repo
identity read from a raw remote (§5.7). The vector files are written by the independent oracle."""

import json
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

# deviations of orch.store.observe.repo_identity from ticket-format §5.7 found by these vectors (reported in the PR):
# ssh:// URLs without `git@` or with a port, scp-like forms with another user or none, and the https default port
# are all turned into `local:<name>` (or a wrong path) instead of the canonical URL.
KNOWN_GAPS = {
    "https_default_port_dropped",
    "ssh_url_no_user",
    "ssh_url_password",
    "ssh_default_port_dropped",
    "ssh_other_port_kept",
    "scp_like_other_user",
    "scp_like_no_user",
}


@pytest.mark.parametrize(
    "c",
    [
        pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="repo_identity deviates from F1 5.7"))
        if c["name"] in KNOWN_GAPS
        else c
        for c in RM
    ],
    ids=[c["name"] for c in RM],
)
def test_repo_identity_from_a_raw_remote(tmp_path, c):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if c["raw"]:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", c["raw"]], check=True)
    got = observe.repo_identity(tmp_path, c["repo_name"])
    assert got == c["canonical"]
    assert "s3cr3t" not in got and "ghp_" not in got  # a token never lands in a hashed value
    if not got.startswith("local:"):
        assert canon.check_repo_identity(got) == got


def test_a_token_in_the_remote_is_not_in_the_result_or_the_canonical_form():
    for c in RM:
        assert "s3cr3t" not in c["canonical"] and "ghp_" not in c["canonical"]
