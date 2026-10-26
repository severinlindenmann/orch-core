"""The F1 gate hash vectors (ticket-format §5.6, §5.7) really pin every input: each case ships the small real ticket
it was derived from, and every inner hash (section, artifact digest, policy, people) and the gate hash itself is
reproduced by ``orch.canon`` from that ticket, with the effective policy checked against ``orch.model.policies``."""

import copy
import json
from pathlib import Path

import pytest

from orch import canon
from orch.model import policies

from . import oracle_f1_gate as og

DIR = Path(__file__).parent.parent / "vectors" / "f1"
CASES = json.loads((DIR / "gate_hash.json").read_text(encoding="utf-8"))["gate_hash"]
GATES = ("requirements", "plan", "verify", "code")
ids = [c["name"] for c in CASES]


def _eff(t, gate):
    return policies.intersect(t["ws_policies"][gate], t["overrides"].get(gate))


@pytest.mark.parametrize("c", CASES, ids=ids)
def test_every_inner_hash_is_derived_from_the_ticket(c):
    t, G, gate = c["ticket"], c["G"], c["gate"]
    for sid, hv in G["sections"].items():
        assert canon.section_hash(t["sections"].get(sid, "")) == hv  # a missing section is the hash of ""
    for name, a in G["artifacts"].items():
        assert canon.artifact_digest(bytes.fromhex(t["artifacts"][name]["bytes_hex"])) == a["digest"]
    for addon, pkg in G["addon_packages"].items():
        assert t["addons"][addon]["package_sha256"] == pkg
    assert canon.policy_hash(gate, c["effective_policy"]) == G["policy_hash"]
    assert canon.people_hash(c["people"]) == G["people_hash"]
    assert canon.gate_hash(G) == c["hash"]
    for s in G["source_sha"]:
        assert canon.check_repo_identity(s["repo"]) == s["repo"]
    assert [s["repo"] for s in G["source_sha"]] == sorted(s["repo"] for s in G["source_sha"])


@pytest.mark.parametrize("c", CASES, ids=ids)
def test_effective_policy_is_the_intersection(c):
    t, gate = c["ticket"], c["gate"]
    assert policies.intersect(t["ws_policies"][gate], t["overrides"].get(gate)) == c["effective_policy"]
    assert policies.named_roles(c["effective_policy"]) == set(c["people"])


@pytest.mark.parametrize("c", CASES, ids=ids)
def test_prior_lists_every_earlier_gate_that_applies(c):
    t, gate = c["ticket"], c["gate"]
    earlier = [g for g in GATES[: GATES.index(gate)] if policies.applies_to(_eff(t, g), t["type"])]
    assert sorted(c["G"]["prior"]) == sorted(earlier)
    for g in earlier:
        want = sorted(t["prior"][g]["counting"][: _eff(t, g)["count"]])
        assert c["G"]["prior"][g] == {"gen": t["prior"][g]["gen"], "approvals": want}
        assert c["G"]["prior"][g]["approvals"] == sorted(c["G"]["prior"][g]["approvals"])


def case(name):
    return next(c for c in CASES if c["name"] == name)


def test_code_prior_names_requirements_plan_and_verify():
    assert sorted(case("code")["G"]["prior"]) == ["plan", "requirements", "verify"]
    # plan has count 2 under the override: its approvals are the first two counting ones, sorted
    assert case("code")["G"]["prior"]["plan"]["approvals"] == [
        "01J9ZP0000000000000000000B",
        "01J9ZP0000000000000000000D",
    ]


def test_a_gate_that_does_not_apply_is_not_in_prior():
    assert sorted(case("verify_plan_not_applicable")["G"]["prior"]) == ["requirements"]


def test_code_people_hash_covers_the_assignees():
    c = case("code")
    assert c["people"] == {"assignees": [og.P_MARA]}  # not: [assignees], independent: true
    t2 = copy.deepcopy(c["ticket"])
    t2["people"]["assignees"].append(og.P_LENA)
    assert canon.gate_hash(og.derive_G(t2, "code")) != c["hash"]


def test_which_roles_each_gate_depends_on():
    t = case("code")["ticket"]

    def hashes(tt):
        return {g: canon.gate_hash(og.derive_G(tt, g)) for g in GATES}

    base = hashes(t)
    for role, changes in (
        ("watchers", set()),
        ("reviewers", {"plan", "verify"}),
        ("assignees", {"plan", "verify", "code"}),
    ):
        t2 = copy.deepcopy(t)
        t2["people"][role] = [og.P_LENA]
        now = hashes(t2)
        # `prior` carries generations and approval ids, not hashes: a changed people hash does not cascade
        assert {g for g in GATES if now[g] != base[g]} == changes, role


def test_the_ticket_owner_token_puts_ticket_owner_in_the_people_hash():
    t = copy.deepcopy(case("requirements")["ticket"])
    t["ws_policies"]["requirements"]["approvers"] = ["owner", "ticket_owner"]
    assert og.people_for(og.derive_policies(t)["requirements"], t) == {"ticket_owner": og.P_SEV}
    assert canon.people_hash({"ticket_owner": og.P_SEV}) == og.derive_G(t, "requirements")["people_hash"]


def test_the_requirements_gate_with_inline_artifact_binds_the_digest():
    c = case("requirements")
    assert sorted(c["G"]["artifacts"]) == ["mock.png"]  # named in the refs of a requirements section
    assert c["G"]["artifacts"]["mock.png"]["kind"] == "screenshot"
    assert sorted(case("verify")["G"]["artifacts"]) == ["mock.png", "run.log"]  # verify binds every file artifact
    assert case("code")["G"]["artifacts"] == {}


def test_two_repos_sort_by_identity_and_ssh_port_is_mapped():
    sl = case("code_two_repos")["G"]["source_sha"]
    assert [s["repo"] for s in sl] == ["https://git.example.com:2222/Acme/infra", "https://github.com/acme/energy-dbt"]


def test_every_g_has_the_fifteen_keys():
    assert all(len(c["G"]) == 15 for c in CASES)
