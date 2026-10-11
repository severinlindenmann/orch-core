"""A human signature needs a person at a terminal: no TTY, a grant in the environment or a wrong passphrase writes
nothing, the prompt shows what the signature covers, and nothing an agent can reach supplies the passphrase."""

from __future__ import annotations

import subprocess
import sys

import pytest

from orch import canon, crypto
from orch.custody import NoPrompt
from orch.ops import human
from orch.ops.base import Context
from tests.ops.humans import PASSPHRASE, make_ticket
from tests.store.helpers import WS


def count(ws, ref="1"):
    return len(ws.events(ref))


def binary(ws, *argv, env=None, stdin=None, session=True):
    """``python -m orch`` in its own session: no controlling terminal, so /dev/tty cannot be opened."""
    e = {"PATH": "/usr/bin:/bin", "HOME": str(ws.tmp), "ORCH_STATE_DIR": str(ws.host_state), **(env or {})}
    if session:
        e.setdefault("ORCH_SESSION", "s_01J9ZP0000000000000000000S")
    return subprocess.run(
        [sys.executable, "-m", "orch", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=ws.root / "tickets",
        env=e,
        timeout=60,
        start_new_session=True,
    )


# ------------------------------------------------------------------------------------------------ the refusals


def test_a_grant_in_the_environment_is_refused_and_asks_nothing(hws, agent, me):
    key = make_ticket(agent)
    n = count(hws)
    hws.provider.requests.clear()
    for argv in (
        ("approve", "requirements", "--ref", key),
        ("request-changes", "plan", "--ref", key, "-m", "x"),
        ("verdict", "pass", "--ref", key),
        ("answer", "Q1", "-m", "x", "--ref", key),
        ("close", key),
        ("reopen", key),
        ("grant",),
        ("grant", "revoke", "gr_01J9ZP0000000000000000000A"),
        ("member", "role", hws.owner.ref, "member"),
        ("member", "remove", hws.owner.ref),
    ):
        r = agent(*argv, "--json")
        assert r.code == 3 and r.err_code == "human_only", (argv, r.out)
    assert hws.provider.requests == [] and count(hws) == n and hws.shown_secrets == []


def test_a_handler_given_a_grant_refuses_even_if_the_cli_let_it_through(hws, agent):
    key = make_ticket(agent)
    ctx = Context(grant=hws.grant, human_presence=True, env=hws.env(grant=False), now=lambda: hws.clock[0])
    from orch.ops.commands import approve
    from orch.ops.errors import OrchError

    with pytest.raises(OrchError) as e:
        approve.OP.handler(ctx, {"gate": "requirements", "ref": key})
    assert e.value.code == "human_only"


def test_no_terminal_is_custody_no_prompt_through_the_production_backend(hws, agent, me, monkeypatch):
    """The real backend (no injected callback) over the real key file: without /dev/tty it fails closed."""
    from orch.custody import get_backend, passphrase

    key = make_ticket(agent)
    monkeypatch.setattr(human, "open_backend", lambda d: get_backend("passphrase", d))

    def no_tty():
        raise NoPrompt("no controlling terminal")

    monkeypatch.setattr(passphrase, "_open_tty", no_tty)
    n = count(hws)
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "custody.no_prompt", r.out
    assert count(hws) == n


def test_the_binary_without_a_terminal_refuses_and_never_reads_a_passphrase_from_anywhere(live_hws, live_agent):
    ws = live_hws
    key = make_ticket(live_agent)
    n = count(ws)
    leaks = {
        "ORCH_PASSPHRASE": PASSPHRASE,
        "PASSPHRASE": PASSPHRASE,
        "ORCH_KEY_PASSPHRASE": PASSPHRASE,
        "ORCH_HUMAN": "1",
        "ORCH_PRESENCE": "1",
    }
    for stdin in (None, PASSPHRASE + "\n"):
        for session in (True, False):
            p = binary(ws, "approve", "requirements", "--ref", key, "--json", env=leaks, stdin=stdin, session=session)
            assert p.returncode == 3 and '"custody.no_prompt"' in p.stdout, (p.stdout, p.stderr)
            assert PASSPHRASE not in p.stdout + p.stderr
    assert count(ws) == n


def test_the_binary_with_a_grant_is_human_only(live_hws, live_agent):
    ws = live_hws
    key = make_ticket(live_agent)
    p = binary(ws, "approve", "requirements", "--ref", key, "--json", env={"ORCH_GRANT": ws.grant})
    assert p.returncode == 3 and '"human_only"' in p.stdout, (p.stdout, p.stderr)


def test_there_is_no_option_that_carries_a_passphrase_or_skips_the_prompt(hws, agent, me):
    key = make_ticket(agent)
    n = count(hws)
    for flag in ("--passphrase", "--password", "--pass", "--yes", "--force", "--no-prompt", "--presence", "--person"):
        r = me("approve", "requirements", "--ref", key, flag, "x")
        assert r.code == 2, flag
    assert count(hws) == n  # nothing was signed
    assert hws.provider.requests == []


def test_a_wrong_passphrase_writes_nothing(hws, agent, me):
    key = make_ticket(agent)
    n = count(hws)
    hws.provider.passphrase = "not the passphrase at all"
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "custody.wrong_passphrase", r.out
    assert count(hws) == n and len(hws.provider.requests) == 1
    r = me("grant", "--json")
    assert r.err_code == "custody.wrong_passphrase" and hws.shown_secrets == []
    hws.provider.passphrase = PASSPHRASE
    assert me("approve", "requirements", "--ref", key).code == 0  # the right one signs


def test_an_empty_passphrase_is_not_an_answer(hws, agent, me):
    key = make_ticket(agent)
    hws.provider.passphrase = ""
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "custody.wrong_passphrase"


def test_a_device_the_log_does_not_know_signs_nothing(hws, agent, me, tmp_path):
    from tests.ops.humans import BackedPerson

    key = make_ticket(agent)
    stranger = BackedPerson(tmp_path / "keys-stranger", hws.provider)
    hws.act_as(stranger)
    hws.provider.requests.clear()
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "role.denied" and hws.provider.requests == []


def test_no_key_on_this_machine(hws, agent, me):
    key = make_ticket(agent)
    (hws.person_dir / "dk.key.json").unlink()
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "custody.no_key" and hws.provider.requests == []
    assert "orch init" in r.doc["error"]["hint"]


def test_a_key_file_others_can_read_is_not_used(hws, agent, me):
    key = make_ticket(agent)
    (hws.person_dir / "dk.key.json").chmod(0o644)
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 1 and hws.provider.requests == []


# ------------------------------------------------------------------------------------------------ the prompt


def test_the_prompt_shows_the_action_the_hash_and_the_decisive_fields(hws, agent, me):
    key = make_ticket(agent)
    hws.provider.shown.clear()
    assert me("approve", "requirements", "--ref", key).code == 0
    (shown,) = hws.provider.shown
    (e,) = [x for x in hws.events("1") if x["type"] == "gate.approved"]
    uid = hws.uid("1")
    payload = canon.person_signing_bytes(WS, uid, e)
    lines = shown.splitlines()
    assert lines[1] == "=== orch: passphrase ===" and lines[2] == f"sha256: {crypto.sha256(payload).hex()[:32]}"
    for want in (
        f"workspace: {WS}",
        f"log: {uid}",
        "type: gate.approved",
        "auth: passphrase",
        "roster_v: 1",
        f"actor.person: {hws.owner.ref}",
        f"actor.device: {hws.owner.device}",
        "gate: requirements",
        f"gate_gen: {e['gate_gen']}",
        f"hash: {e['hash']}",
        f"policy_hash: {e['policy_hash']}",
    ):
        assert want in lines, want
    assert any(
        x.startswith("note (caller text, not signed)") and "approve requirements of DEMO-0001" in x for x in lines
    )


def test_the_prompt_of_a_grant_shows_its_terms_and_the_secret_never_appears_in_it(hws, me):
    hws.provider.shown.clear()
    assert me("grant", "--hours", "3", "--scope", "all", "--verbs", "log,ask").code == 0
    (shown,) = hws.provider.shown
    for want in ("type: grant.issued", "scope: all", "hours: 3", "verbs[1]: log", "verbs[2]: ask", "log: workspace"):
        assert want in shown.splitlines(), want
    from tests.ops.test_grant import shown_value

    assert shown_value(hws).partition(".")[2] not in shown


def test_a_decision_that_the_log_would_refuse_is_never_prompted(hws, agent, me):
    key = make_ticket(agent)
    hws.provider.requests.clear()
    assert me("verdict", "pass", "--ref", key, "--json").err_code == "transition.refused"  # not in testing
    assert me("close", key, "--duplicate-of", "7", "--json").code != 0
    assert hws.provider.requests == []


def test_a_workspace_without_a_key_directory_is_not_created_by_looking(hws, agent, me):
    key = make_ticket(agent)
    gone = hws.person_dir
    import shutil

    shutil.rmtree(gone)
    assert me("approve", "requirements", "--ref", key, "--json").err_code == "custody.no_key"
    assert not gone.exists()  # opening a backend would have created it


def test_an_empty_grant_variable_still_counts_as_a_grant(hws, agent):
    key = make_ticket(agent)
    from tests.ops.helpers import Cli

    n = count(hws)
    r = Cli(hws, grant=False)("approve", "requirements", "--ref", key, "--json", ORCH_GRANT="")
    assert r.code == 3 and r.err_code == "human_only" and count(hws) == n and hws.provider.requests == []
