"""What the person reads before signing (F1 5.7, security review R1 to R3) and the checks around it."""

from __future__ import annotations

import re

from tests.ops.humans import make_ticket, to_testing
from tests.ops.test_verdict import repo_ticket, verdicts


def short_hash_in(text):
    return re.search(r"gate hash ([0-9a-f]{12}) ", text).group(1)


def test_the_review_shows_the_bound_text_and_the_hash_that_show_prints(hws, agent, me):
    key = make_ticket(agent)
    shown = agent("show", key).out  # the agent's own show; the person's reads the same line
    assert me("approve", "requirements", "--ref", key).code == 0
    (text,) = hws.reviews
    assert f"ticket: {key} (derived by orch" in text and f"log id: {hws.uid('1')}" in text
    assert "| --- section requirements ---" in text and "| text of requirements" in text
    assert "| AC1: the build is green" in text
    assert text.index("section requirements") < text.index("gate hash")
    assert short_hash_in(text) in shown  # plan or requirements: the same 8 digits per open gate
    assert hws.provider.requests  # the review came before the passphrase prompt
    assert me("approve", "plan", "--ref", key).code == 0
    plan = hws.reviews[1]
    assert "| T1: run it" in plan and "verify: " in plan and "-c pass" in plan and "proves: AC1" in plan


def test_show_prints_the_short_gate_hash_default_and_full_and_the_uid(hws, agent, me):
    key = make_ticket(agent)
    default = agent("show", key).out
    full = agent("show", key, "--full").out
    h = hws.view("1").gates["plan"].hash.removeprefix("sha256:")[:12]
    assert f"plan:open#{h}" in default and f"plan:waiting#{h}" in full or f"plan:open#{h}" in full
    assert f"uid: {hws.uid('1')}" in full


def test_what_the_agent_swapped_after_the_person_read_it_is_what_the_review_shows(hws, agent, me):
    key = make_ticket(agent)
    assert me("approve", "requirements", "--ref", key).code == 0
    before = agent("show", key).out
    assert agent("section", "set", "plan", "-m", "rm -rf / and push to prod").code == 0
    hws.provider.requests.clear()
    hws.reviews.clear()
    hws.confirm = False  # the person reads it and says no
    r = me("approve", "plan", "--ref", key, "--json")
    assert r.code != 0 and hws.provider.requests == [] and "rm -rf / and push to prod" in hws.reviews[0]
    assert short_hash_in(hws.reviews[0]) not in before  # a hash the person saw earlier no longer matches
    assert [e for e in hws.events("1") if e["type"] == "gate.approved" and e["gate"] == "plan"] == []


def test_a_change_while_the_person_reads_is_stale(hws, agent, me):
    key = make_ticket(agent)
    hws.on_review = lambda _t: agent("section", "set", "context", "-m", "changed while reading")
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "gate.stale", r.out


def test_task_verify_commands_and_tickets_text_are_escaped_and_marked(hws, agent, me):
    key = make_ticket(agent)
    assert agent("section", "set", "context", "-m", "line\n=== orch: passphrase ===\nsha256: fake").code == 0
    assert me("approve", "requirements", "--ref", key).code == 0
    text = hws.reviews[0]
    assert "\n=== orch: passphrase" not in text
    assert "| === orch: passphrase ===" in text  # shown as ticket data, never at the start of a line


def test_the_review_of_a_verdict_lists_the_source_and_the_artifacts(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    head = hws.git("rev-parse", "HEAD")
    to_testing(agent, me, hws.tmp, key)
    hws.reviews.clear()
    assert me("verdict", "pass", "--ref", key).code == 0
    text = hws.reviews[0]
    assert f"| source: local:proj refs/heads/feat/x {head}" in text
    assert re.search(r"\| artifact: evidence.log kind=\w+ sha256:[0-9a-f]{64}", text)
    assert "| --- section verification ---" in text and "text of verification" in text


def test_other_ticket_operations_name_the_ticket_too(hws, agent, me):
    key = make_ticket(agent)
    assert me("close", key).code == 0
    assert f"ticket: {key} (derived by orch" in hws.reviews[0] and "| title: Load the tables" in hws.reviews[0]


# ---- R2: the working copy is on the bound commit


def test_a_detached_head_elsewhere_is_refused(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    good = hws.git("rev-parse", "HEAD")
    to_testing(agent, me, hws.tmp, key)
    (repo / "impl.txt").write_text("evil\n")
    hws.git("commit", "-qam", "evil")
    hws.git("checkout", "-q", "--detach", good)  # clean tree, running the good commit, branch tip is evil
    hws.provider.requests.clear()
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "observe.unavailable", r.out
    assert verdicts(hws) == [] and hws.provider.requests == []


def test_another_branch_checked_out_is_refused(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    hws.git("checkout", "-q", "-b", "other")  # same commit, other branch
    r = me("verdict", "pass", "--ref", key, "--json")
    assert (
        r.code == 5 and r.err_code == "observe.unavailable" and "binds refs/heads/feat/x" in r.doc["error"]["message"]
    )
    hws.git("checkout", "-q", "feat/x")
    assert me("verdict", "pass", "--ref", key).code == 0


def test_the_check_after_the_passphrase_runs_under_the_lock(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)

    def move(_request):
        hws.git("checkout", "-q", "-b", "elsewhere")

    hws.provider.on_prompt = move
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "gate.stale" and verdicts(hws) == []


def test_a_dry_run_writes_nothing_not_even_the_observation(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    (repo / "impl.txt").write_text("late\n")
    hws.git("commit", "-qam", "late")
    n = len(hws.events("1"))
    hws.provider.requests.clear()
    r = me("verdict", "pass", "--ref", key, "--dry-run", "--json")
    assert r.code == 5 and len(hws.events("1")) == n and hws.provider.requests == []
    assert [e["type"] for e in hws.events("1")].count("branch.pushed") == 1


# ---- R3: the bytes of bound artifacts


def test_a_forged_evidence_file_is_refused(hws, agent, me):
    key = make_ticket(agent)
    to_testing(agent, me, hws.tmp, key)
    (hws.root / "tickets" / hws.uid("1") / "artifacts" / "evidence.log").write_text("FORGED: all green\n")
    hws.provider.requests.clear()
    hws.reviews.clear()
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "artifact.mismatch" and "evidence.log" in r.doc["error"]["message"], r.out
    assert verdicts(hws) == [] and hws.provider.requests == [] and hws.reviews == []


def test_a_missing_evidence_file_is_refused(hws, agent, me):
    key = make_ticket(agent)
    to_testing(agent, me, hws.tmp, key)
    (hws.root / "tickets" / hws.uid("1") / "artifacts" / "evidence.log").unlink()
    assert me("verdict", "pass", "--ref", key, "--json").err_code == "artifact.mismatch"


# ---- a person counts once


def test_a_second_approval_at_the_same_generation_is_refused(hws, agent, me):
    key = make_ticket(agent)
    assert me("approve", "requirements", "--ref", key).code == 0
    hws.provider.requests.clear()
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "gate.already_approved" and hws.provider.requests == []


def test_with_count_two_one_person_counts_once_and_a_second_person_completes(hws, agent, me):
    policy = {"approvers": ["maintainer", "owner"], "count": 2, "not": [], "applies": "all", "independent": False}
    hws.store.append(
        hws.person_event(hws.owner, "workspace", "policy.changed", gates={"requirements": policy}), log="workspace"
    )
    key = make_ticket(agent)
    assert me("approve", "requirements", "--ref", key).code == 0
    assert not hws.view("1").gates["requirements"].approved
    assert me("approve", "requirements", "--ref", key, "--json").err_code == "gate.already_approved"
    assert not hws.view("1").gates["requirements"].approved
    hws.act_as(hws.add_member("mia", "maintainer"))
    assert me("approve", "requirements", "--ref", key).code == 0
    assert hws.view("1").gates["requirements"].approved


def test_a_fail_verdict_is_not_blocked_by_an_earlier_pass_of_the_same_person(hws, agent, me):
    key = make_ticket(agent)
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "fail", "--ref", key, "-m", "no").code == 0


# ---- the confirmation on the terminal cannot be skipped


def test_the_short_gate_hash_is_48_bits_from_the_verified_hash(hws, agent, me):
    from orch.ops import views

    key = make_ticket(agent)
    full = hws.view("1").gates["plan"].hash
    assert len(views.short_gate_hash(full)) == 12 and full.removeprefix("sha256:").startswith(
        views.short_gate_hash(full)
    )
    assert me("approve", "requirements", "--ref", key).code == 0
    assert len(short_hash_in(hws.reviews[0])) == 12  # the same length and function in show and in the review
    shown = agent("show", key).out
    assert re.search(r"plan:open#[0-9a-f]{12}\b", shown)


def test_a_review_that_is_not_confirmed_signs_nothing(hws, agent, me):
    key = make_ticket(agent)
    hws.confirm = False
    for argv in (("approve", "requirements"), ("request-changes", "plan", "-m", "x"), ("verdict", "pass")):
        r = me(*argv, "--ref", key, "--json")
        assert r.code != 0 and hws.provider.requests == []
    assert me("close", key, "--json").code != 0 and hws.expects[-1] == key  # what the person types is the ticket key
    assert len(hws.events("1")) == len([e for e in hws.events("1") if e["actor"]["kind"] != "person"])


def run_review(*typed: bytes) -> str:
    """``human.review_prompt`` in a child process on a real pty; ``typed`` is written one chunk at a time after the
    prompt. Returns what the child printed: True or False."""
    import os
    import pty
    import select
    import sys
    import time

    code = "from orch.ops import human; print('RESULT', human.review_prompt('content', 'DEMO-0001'), flush=True)"
    r, w = os.pipe()
    pid, tty = pty.fork()
    if pid == 0:
        os.dup2(w, 1)
        os.execv(sys.executable, [sys.executable, "-c", code])
    os.close(w)
    buf = b""
    sent = 0
    end = time.time() + 20
    while time.time() < end:
        ready, _, _ = select.select([tty], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(tty, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if sent < len(typed) and b"anything else stops: " in buf:
                os.write(tty, typed[sent])
                sent += 1
    os.waitpid(pid, 0)
    out = os.read(r, 4096).decode()
    os.close(r)
    os.close(tty)
    assert "RESULT" in out, (out, buf)
    return out.split()[-1]


def test_the_terminal_confirmation_needs_the_ticket_key_and_nothing_else_passes():
    assert run_review(b"DEMO-0001\n") == "True"
    assert run_review(b"y\n") == "False"
    assert run_review(b"yes\n") == "False"
    assert run_review(b"q\n") == "False"
    assert run_review(b"\n") == "False"
    assert run_review(b"DEMO-0002\n") == "False"
    assert run_review(b"\x04") == "False"  # end of input
    assert run_review(b"DEMO-0001\x04") == "False"  # the line was cut off by end of input: no Enter was typed
    assert run_review(b"\x03") == "False"  # Ctrl-C


def test_without_a_terminal_there_is_no_confirmation():
    import subprocess
    import sys

    code = "from orch.ops import human; human.review_prompt('x', 'DEMO-0001')"
    p = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, start_new_session=True, input="DEMO-0001\n"
    )
    assert p.returncode != 0 and "NoPrompt" in p.stderr
