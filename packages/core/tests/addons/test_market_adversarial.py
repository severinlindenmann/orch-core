"""Adversarial cases for ``orch.market/1`` from the L2 security review (tag ``l2-market``, findings L2M-n in
``~/orch-dev/state/l2-review-market.md``).

Every test here FAILS against the reference checker of PR #980 as reviewed (head c157efad). Each one states the answer
the contract should give. When a finding is fixed, the case moves into ``make_vectors_market.py`` as a vector (with
the same name) and the test here is deleted. A finding the owner decides to accept is deleted here with that decision
recorded in the review.

T0: ``uv run pytest tests/addons/test_market_adversarial.py -q``
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.addons import make_vectors_market as mk
from tests.addons import market_ref as ref

OFFICIAL, OTHER = mk.OFFICIAL, mk.OTHER
H64 = "a" * 64
INTEGRITY = "sha512-" + "A" * 86 + "=="


def install(index: dict[str, Any], files: dict[str, str], addon: str, **cx: Any) -> dict[str, Any]:
    return ref.run_case(
        {"check": "install", "args": {"index": index, "id": addon, "files": files, "ctx": mk.ctx(**cx)}}
    )


def refused(result: dict[str, Any], *codes: str) -> bool:
    return result.get("refuse") in codes


# ------------------------------------------------------------------------------------------------ L2M-5 addresses


@pytest.mark.parametrize(
    "ip",
    [
        "64:ff9b::7f00:1",  # NAT64 well-known prefix (RFC 6052) embedding 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 embedding 169.254.169.254 (cloud metadata); DNS64 synthesises these
        "64:ff9b::a00:1",  # NAT64 embedding 10.0.0.1
        "::127.0.0.1",  # IPv4-compatible (deprecated ::/96)
        "::ffff:0:7f00:1",  # IPv4-translated (SIIT, ::ffff:0:0/96) embedding 127.0.0.1
        "::ffff:0:a9fe:a9fe",  # IPv4-translated embedding the metadata address
        "fec0::1",  # site-local (deprecated, RFC 3879): private by intent
    ],
)
def test_l2m5_address_embedded_ipv4_and_site_local_refused(ip):
    assert ref.run_case({"check": "address", "args": {"ip": ip}}) == {"refuse": "market.address_refused"}


# ------------------------------------------------------------------------------------------------ L2M-6 node lock


def _node_lock(extra: dict[str, Any], *, version: Any = 3) -> str:
    return json.dumps(
        {
            "lockfileVersion": version,
            "packages": {
                "": {"name": "x", "dependencies": {"left-pad": "1.3.0"}},
                "node_modules/left-pad": {
                    "version": "1.3.0",
                    "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                    "integrity": INTEGRITY,
                },
                **extra,
            },
        }
    )


def _pkg(version: str, resolved: str, integrity: str = INTEGRITY) -> dict[str, Any]:
    return {"version": version, "resolved": resolved, "integrity": integrity}


@pytest.mark.parametrize(
    "case,extra",
    [
        # (a) a package key that walks out of node_modules: npm would place the files outside the environment
        (
            "key_traversal",
            {"node_modules/../../../escape": _pkg("1.0.0", "https://registry.npmjs.org/a/-/a-1.0.0.tgz")},
        ),
        ("key_empty_segment", {"node_modules//etc": _pkg("1.0.0", "https://registry.npmjs.org/a/-/a-1.0.0.tgz")}),
        # (b) the key and version the review shows are not what installs
        (
            "resolved_names_another_package",
            {"node_modules/lodash": _pkg("4.17.21", "https://registry.npmjs.org/evil-pkg/-/evil-pkg-6.6.6.tgz")},
        ),
        (
            "resolved_other_version",
            {"node_modules/lodash": _pkg("4.17.21", "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz")},
        ),
        # (c) dot segments in the URL path
        (
            "resolved_dot_dot",
            {"node_modules/a": _pkg("1.0.0", "https://registry.npmjs.org/../evil/-/a-1.0.0.tgz")},
        ),
        # (d) integrity that pins nothing, or not exactly one sha512
        (
            "integrity_empty_sha512",
            {"node_modules/b": _pkg("1.0.0", "https://registry.npmjs.org/b/-/b-1.0.0.tgz", "sha512-")},
        ),
        (
            "integrity_short",
            {"node_modules/b": _pkg("1.0.0", "https://registry.npmjs.org/b/-/b-1.0.0.tgz", "sha512-AAAA")},
        ),
        (
            "integrity_two_hashes",
            {"node_modules/b": _pkg("1.0.0", "https://registry.npmjs.org/b/-/b-1.0.0.tgz", INTEGRITY + " sha1-AAAA")},
        ),
    ],
)
def test_l2m6_node_lock_entries_bound_to_their_key_and_pinned(case, extra):
    res = ref.run_case({"check": "lock", "args": {"kind": "node-package-lock", "text": _node_lock(extra)}})
    assert res == {"refuse": "market.lock_invalid"}, case


def test_l2m6_node_lock_version_must_be_the_integer_3():
    text = _node_lock({}).replace('"lockfileVersion": 3', '"lockfileVersion": 3.0')
    assert ref.run_case({"check": "lock", "args": {"kind": "node-package-lock", "text": text}}) == {
        "refuse": "market.lock_invalid"
    }


# ------------------------------------------------------------------------------------------------ L2M-2 reserved ids


def test_l2m2_other_marketplace_cannot_claim_an_official_id():
    """Spec section 1: every id in the official marketplace is reserved. The checker only knows the built-in list, so
    a community marketplace (post #287) can publish its own ``github``. The official ids must be known to core even
    when no verified official index is cached (core ships them), so the context names them."""
    f = mk.files_for("github", caps=["git_push", "network"])
    e = mk.entry("github", f, caps=["git_push", "network"])
    res = install(mk.index(OTHER, [e]), f, "github", official_ids=["github", "merge-queue"])
    assert res == {"refuse": "market.reserved_id"}


def test_l2m2_official_entry_cannot_shadow_a_builtin_id():
    """An official entry named like a built-in (``dashboard``) would install a second package under a built-in name.
    Only ids core lists as moved to the marketplace (G6) may appear there."""
    f = mk.files_for("dashboard")
    e = mk.entry("dashboard", f, signer="official", market=OFFICIAL)
    # signed index (L2M-1): an unsigned official index is refused at step 2, before the entry is looked at
    res = install(
        mk.oindex(OFFICIAL, [e], name="orch-marketplace"), f, "dashboard", market_repo=OFFICIAL, isolation=False
    )
    assert res == {"refuse": "market.reserved_id"}


# ------------------------------------------------------------------------------------------------ L2M-3 URL identity


def test_l2m3_index_source_with_git_suffix_is_not_canonical():
    """Spec 4.1: the normal form has no trailing ``.git``. The schema accepts it, so ``/acme/m`` and ``/acme/m.git``
    (one repo) are two trust keys."""
    idx = mk.index(OTHER + ".git", [])
    assert ref.run_case({"check": "index", "args": {"index": idx}}) == {"refuse": "market.malformed"}


def test_l2m3_alias_url_does_not_reset_tofu():
    """The owner trusted jane's key for ``acme/orch-market``; the same repo added again as ``acme/orch-market.git``
    must not start a fresh TOFU slot in which mallory's key is 'first use'."""
    alias = OTHER + ".git"
    f = mk.files_for("jira", caps=["network"])
    e = mk.entry("jira", f, caps=["network"], signer="mallory", market=alias)
    res = install(mk.index(alias, [e]), f, "jira", market_repo=alias, known_keys={f"{OTHER}|jira": mk.PUB["jane"]})
    assert refused(res, "market.malformed", "market.publisher_key_changed"), res


def test_l2m3_alias_url_does_not_reset_the_rollback_floor():
    alias = OTHER + ".git"
    f = mk.files_for("standup", version="1.0.0", caps=["network"])
    e = mk.entry("standup", f, version="1.0.0", caps=["network"])
    res = install(
        mk.index(alias, [e], version=3),
        f,
        "standup",
        market_repo=alias,
        last_version=None,
        seen={f"{OTHER}|standup": "2.0.0"},
    )
    assert refused(res, "market.malformed", "market.rollback"), res


# ------------------------------------------------------------------------------------------------ L2M-10 version reuse


def test_l2m10_same_version_other_bytes_is_refused():
    """1.0.0 is installed with digest A. An entry 1.0.0 with digest B (a re-tagged release, or an older signed entry
    of the same version replayed) is accepted today because only a *lower* version is refused."""
    f = mk.files_for("standup", caps=["network"], extra={"other.py": "x = 1\n"})
    e = mk.entry("standup", f, caps=["network"])
    installed = {"standup": {"origin": OTHER, "version": "1.0.0", "package_sha256": "sha256:" + "0" * 64}}
    res = install(mk.index(OTHER, [e]), f, "standup", installed=installed)
    assert refused(res, "market.rollback", "market.version_reused"), res


# ------------------------------------------------------------------------------------------------ L2M-8 runtime


def test_l2m8_runtime_must_agree_with_the_manifest():
    """The index's ``runtime`` decides which file is installed as the lockfile and whether dependencies install at
    all, but it is not in the manifest, so not in ``addon.granted``. A manifest that declares no runtime with an
    index that adds one must be refused (manifest_mismatch), so env install is driven by signed bytes only."""
    f = mk.files_for("jira", caps=["network"], extra={"requirements.txt": f"requests==2.32.3 --hash=sha256:{H64}\n"})
    e = mk.entry("jira", f, caps=["network"], runtime={"kind": "python-requirements", "lock": "requirements.txt"})
    assert install(mk.index(OTHER, [e]), f, "jira") == {"refuse": "market.manifest_mismatch"}


# ------------------------------------------------------------------------------------------------ L2M-1 index signature


def test_l2m1_official_index_without_index_signature_is_refused():
    """The official shelf replaces the signed catalog (addon-platform section 3: signed, seq, expires, revoked). An
    official index must carry a signature by the official key over its URL, version and entry list; without it,
    whoever controls the git repo can freeze (version 2**53-1), serve old signed entries to fresh hosts and edit the
    reserved-id list."""
    f = mk.files_for("github", caps=["git_push", "network"])
    e = mk.entry("github", f, caps=["git_push", "network"], signer="official", market=OFFICIAL)
    idx = mk.index(OFFICIAL, [e], version=9007199254740991, name="orch-marketplace")
    res = install(idx, f, "github", market_repo=OFFICIAL, isolation=False)
    assert refused(res, "market.official_unsigned", "market.index_unsigned"), res
