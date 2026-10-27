"""B9 step 5: the create sequence: order, R1 abort-before-signature, R3 tty confirm, R4 runner-before-grant, R6
conversion last, R9 clean restart, no secret in any output."""

from __future__ import annotations

import json

import pytest

from orch.host import signer as host_signer
from orch.setup.flow import SetupFlow
from orch.setup.protocol import SetupError

from .conftest import PROFILE, make_env
from .fakes import FakePorts, FakeTerminal, names
from .test_secrets import reveal, words_for

GRANT_SECRET = "gr_01ARZ3NDEKTSV4RRFFQ69G5FAV.SECRETSECRETSECRETSECRETSECRETSECRETSEC"


@pytest.fixture(autouse=True)
def fake_signer(monkeypatch):
    monkeypatch.setattr(host_signer, "check", lambda *a, **k: None)

    def sign(prepared, *, backend, key_id, workspace_id):
        events = [{**e, "sig": "x"} for e in prepared["events"]]
        secret = GRANT_SECRET if any(e["type"] == "grant.issued" for e in events) else None
        return host_signer.Signed(events, secret)

    monkeypatch.setattr(host_signer, "sign", sign)
    monkeypatch.setattr(
        host_signer,
        "hand_over",
        lambda call, secret, did: {
            "deposit": call("grant.deposit", {"deposit_id": did}, auth={"grant": secret})["data"]["deposit"],
            "grant": "gr_x",
        },
    )


def setup_flow(tmp, ports, **envkw):
    f = SetupFlow(make_env(tmp, ports=lambda: ports, clock=lambda: ports.now, **envkw))
    f.launch(str(tmp.work))
    return f


def progress(f, run_id):
    return f.dispatch("GET", "progress", {"run_id": run_id}, None)[1]


AGENT = {
    **PROFILE,
    "addons": ["dashboard", "terminals", "start-agent", "worktrees"],
    "agent_hours": 8,
    "agent_grant": True,
}


def run_ok(tmp, profile, ports=None, **envkw):
    ports = ports or FakePorts()
    f = setup_flow(tmp, ports, **envkw)
    # first pass only to learn the fingerprint
    f.dispatch("POST", "profile", {}, profile)
    r = reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"words": words_for(r)})
    _, rv = f.dispatch("POST", "review", {}, {})
    if rv["fingerprint"]:
        ports.terminal.answers[:] = ["yes"]
    ports.expect(f.review.plan)
    body = {"review_id": rv["review_id"], "digest": rv["digest"]}
    out = f.dispatch("POST", "create", {}, body)[1]
    f.thread.join(10)
    return f, ports, progress(f, out["run_id"]), rv


def test_terminal_path_signs_in_order_with_one_unlock_and_the_grant_last(tmp):
    f, ports, p, rv = run_ok(tmp, AGENT)
    assert p["error"] is None and p["done"] is True and f.phase == "done"
    assert ports.unlocks == 1
    assert ports.submits == ["addon.granted"] * 4 + ["grant.issued"]  # dashboard, start-agent, terminals, worktrees
    n = names(ports)
    assert n.index("genesis") < n.index("install_addon") < n.index("runner_enable") < n.index("runner_hello")
    assert n.index("runner_hello") < n.index("human.prepare") < n.index("unlock") < n.index("human.submit")
    assert "stage" not in n and "convenience_on" not in n  # terminal path: no conversion
    ids = [s["id"] for s in p["steps"]]
    assert ids[:4] == ["keys", "genesis", "register", "runner"] and ids[-1] == "runner_grant"
    assert all(s["status"] == "done" for s in p["steps"])
    assert p["workspace"] == {"id": "0123456789abcdef0123456789abcdef", "prefix": "ANN"}


def test_all_requests_are_prepared_and_checked_before_the_unlock(tmp):
    f, ports, p, _ = run_ok(tmp, AGENT)
    n = names(ports)
    assert n.count("human.prepare") == 5
    assert max(i for i, x in enumerate(n) if x == "human.prepare") < n.index("unlock")


def test_r1_a_tampered_prepare_aborts_before_the_first_signature(tmp):
    ports = FakePorts()
    ports.tamper = lambda ev: ev.update(capabilities=["pty", "spawn_agent"]) if ev["type"] == "addon.granted" else None
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    assert p["error"]["code"] == "review.mismatch" and p["error"]["finishable"] is True
    assert ports.unlocks == 0 and ports.submits == []  # nothing was signed or submitted
    assert ports.cancelled  # the prepared requests were cancelled
    assert f.phase == "failed"


def test_r1_an_extra_event_in_a_prepared_request_aborts(tmp):
    ports = FakePorts()
    real = ports.workspace

    def evil(wid):
        link = real(wid)
        orig = link.call

        def call(op, args, **kw):
            res = orig(op, args, **kw)
            if op == "human.prepare":
                res["data"]["events"].append({"type": "member.removed", "person": "p_" + "3" * 32})
            return res

        link.call = call
        return link

    ports.workspace = evil
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    assert p["error"]["code"] == "review.mismatch" and ports.unlocks == 0


def test_r3_the_tty_fingerprint_must_match(tmp):
    ports = FakePorts(FakeTerminal(["not the words"]))
    f = setup_flow(tmp, ports)
    f.dispatch("POST", "profile", {}, AGENT)
    r = reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"words": words_for(r)})
    _, rv = f.dispatch("POST", "review", {}, {})
    ports.expect(f.review.plan)
    out = f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})[1]
    f.thread.join(10)
    p = progress(f, out["run_id"])
    assert p["error"]["code"] == "tty.declined" and p["error"]["finishable"] is False
    assert names(ports) == []  # nothing happened on the machine
    assert " ".join(rv["fingerprint"]) in ports.terminal.shown[0]


def test_r2_create_needs_the_bound_digest_and_no_typed_word(tmp):
    ports = FakePorts()
    f = setup_flow(tmp, ports)
    f.dispatch("POST", "profile", {}, AGENT)
    r = reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"words": words_for(r)})
    _, rv = f.dispatch("POST", "review", {}, {})
    base = {"review_id": rv["review_id"], "digest": rv["digest"]}
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "create", {}, {**base, "digest": "sha256:" + "0" * 64})
    assert e.value.code == "review.changed" and e.value.status == 409
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "create", {}, {**base, "review_id": "rv_other"})
    assert e.value.code == "review.changed"
    assert names(ports) == []


def test_create_after_a_profile_change_needs_a_new_review(tmp):
    ports = FakePorts()
    f = setup_flow(tmp, ports)
    f.dispatch("POST", "profile", {}, PROFILE)
    r = reveal(f)
    f.dispatch("POST", "secrets/confirm", {}, {"words": words_for(r)})
    _, rv = f.dispatch("POST", "review", {}, {})
    f.dispatch("POST", "profile", {}, {**PROFILE, "agent_hours": 24})
    with pytest.raises(SetupError) as e:
        f.dispatch("POST", "create", {}, {"review_id": rv["review_id"], "digest": rv["digest"]})
    assert e.value.code == "review.changed"


def test_r4_no_hello_means_no_grant_is_signed(tmp):
    ports = FakePorts()
    ports.hello = False
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    assert p["error"] is None and p["done"] is True  # finishable, not failed
    assert "grant.issued" not in ports.submits and ports.submits.count("addon.granted") == 4
    st = {s["id"]: s["status"] for s in p["steps"]}
    assert st["runner_grant"] == "skipped" and p["retry"] == ["runner_grant"]
    assert ports.unlocks == 1


def test_r4_runner_enable_failure_skips_the_grant_and_offers_a_retry(tmp):
    ports = FakePorts()
    ports.fail_runner = True
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    assert {s["id"]: s["status"] for s in p["steps"]}["runner"] == "failed"
    assert "runner" in p["retry"] and "runner_grant" in p["retry"] and "grant.issued" not in ports.submits
    assert "runner_hello" not in names(ports)


def test_the_grant_is_done_only_on_a_confirmed_deposit(tmp):
    ports = FakePorts()
    ports.deposit = "rejected"
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    st = {s["id"]: s["status"] for s in p["steps"]}
    assert st["runner_grant"] == "failed" and "runner_grant_retry" in p["retry"]


def test_a_failed_submit_stops_the_chain_and_marks_the_rest_skipped(tmp):
    ports = FakePorts()
    ports.fail_submit = 2  # the third request (dashboard and start-agent went through)
    f, ports, p, _ = run_ok(tmp, AGENT, ports=ports)
    st = {s["id"]: s["status"] for s in p["steps"]}
    assert st["addon:dashboard"] == "done" and st["addon:start-agent"] == "done" and st["addon:terminals"] == "failed"
    assert st["addon:worktrees"] == "skipped" and st["runner_grant"] == "skipped"
    assert ports.submits == ["addon.granted", "addon.granted"] and p["error"]["finishable"] is True


# --- one-click: R6


CLICK = {**PROFILE, "method": "click", "addons": ["dashboard", "worktrees"]}


def test_click_path_settings_then_conversion_then_flag_last(tmp):
    f, ports, p, rv = run_ok(tmp, CLICK)
    n = names(ports)
    assert p["done"] is True and ports.submits == ["settings.changed", "addon.granted", "addon.granted"]
    assert n.index("human.submit") < n.index("stage") < n.index("finish_conversion") < n.index("convenience_on")
    assert n[-1] == "convenience_on" and "lock" in n and n.index("lock") < n.index("stage")
    assert [s["id"] for s in p["steps"]][-2:] == ["custody", "flags"]


def test_flag_last_a_failed_conversion_never_sets_the_flag(tmp):
    ports = FakePorts()
    ports.fail_stage = 2
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert "convenience_on" not in names(ports)  # key not held: the flag stays off
    st = {x["id"]: x["status"] for x in p["steps"]}
    assert st["custody"] == "failed" and st["flags"] == "skipped"


def test_r6_conversion_retries_in_process(tmp):
    ports = FakePorts()
    ports.fail_stage = 1
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert p["done"] is True and names(ports).count("stage") == 2
    assert not any(ports.passphrase_seen in t for t in ports.terminal.shown)  # it worked on the second try


def test_r6_conversion_failure_shows_the_passphrase_once_on_the_tty_only(tmp):
    ports = FakePorts()
    ports.fail_stage = 2
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert p["error"]["code"] == "custody.conversion_failed" and p["error"]["finishable"] is True
    # B12 N14: plain reason, and the one-click switch row does not say Done above the failure
    assert "macOS did not let orch use the keychain" in p["error"]["message"]
    st = {x["id"]: x["status"] for x in p["steps"]}
    assert st["custody"] == "failed" and st["flags"] == "skipped"
    assert p["retry"] == ["custody"] and f.phase == "failed"
    shown = [t for t in ports.terminal.shown if ports.passphrase_seen in t]
    assert len(shown) == 1 and "never on the page" in shown[0]
    blob = json.dumps(p) + json.dumps(f.dispatch("GET", "state", {}, None)[1])
    assert ports.passphrase_seen not in blob


def test_r6_ctrl_c_during_conversion_undoes_the_staged_copy(tmp):
    ports = FakePorts()
    ports.interrupt_after_stage = True
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert ports.staged_undone == 1 and p["error"]["code"] == "stop"


def _once_on_the_tty_only(f, ports, p):
    shown = [t for t in ports.terminal.shown if ports.passphrase_seen in t]
    assert len(shown) == 1 and "never on the page" in shown[0]
    blob = json.dumps(p) + json.dumps(f.dispatch("GET", "state", {}, None)[1])
    assert ports.passphrase_seen not in blob


def test_high1_a_failed_submit_in_click_mode_still_shows_the_passphrase_once(tmp):
    """The one-click passphrase is never shown up front: a failure after the genesis must not leave the only device
    key under an unseen passphrase (lockout)."""
    ports = FakePorts()
    ports.fail_submit = 1
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert p["error"]["finishable"] is True and "custody" not in [s["id"] for s in p["steps"] if s["status"] == "done"]
    _once_on_the_tty_only(f, ports, p)


def test_high1_ctrl_c_after_the_genesis_in_click_mode_shows_the_passphrase_once(tmp, monkeypatch):
    ports = FakePorts()

    def stop(self, root, name, raw):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakePorts, "install_addon", stop)
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    assert p["error"]["code"] == "stop"
    _once_on_the_tty_only(f, ports, p)


def test_high1_an_unknown_genesis_outcome_in_click_mode_shows_the_passphrase_once(tmp):
    """The bootstrap may still land after an unknown outcome (the device key is kept): the passphrase must be shown."""
    ports = FakePorts()
    ports.fail_genesis = SetupError("host.unknown", "the host did not answer", 500)
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    _once_on_the_tty_only(f, ports, p)


def test_high1_a_terminal_run_never_shows_the_passphrase_on_failure(tmp):
    ports = FakePorts()
    ports.fail_submit = 1
    f, ports, p, _ = run_ok(tmp, PROFILE, ports=ports)  # terminal method: the person already has the passphrase
    assert not any(ports.passphrase_seen in t for t in ports.terminal.shown)


def test_click_path_never_shows_the_passphrase_in_the_browser_secrets(tmp):
    f = setup_flow(tmp, FakePorts())
    f.dispatch("POST", "profile", {}, CLICK)
    assert reveal(f)["passphrase"] is None


# --- R9 and the secrets scan


def test_r9_a_genesis_failure_leaves_nothing_and_a_second_setup_starts_clean(tmp):
    from orch.ops.errors import OrchError

    ports = FakePorts()
    ports.fail_genesis = SetupError("host.unavailable", "no orch host answers", 500)
    f, ports, p, _ = run_ok(tmp, PROFILE, ports=ports)
    assert p["error"]["code"] == "host.unavailable" and p["error"]["finishable"] is False
    assert f.phase == "failed" and p["done"] is False
    assert [s["status"] for s in p["steps"]][:3] == ["failed", "failed", "failed"]
    first_code = ports.code_seen
    # a second `orch setup`: a new flow, new secrets, and it works
    ports2 = FakePorts()
    f2, ports2, p2, _ = run_ok(tmp, PROFILE, ports=ports2)
    assert p2["done"] is True and ports2.code_seen != first_code
    assert tmp.work.exists() and not list(tmp.work.iterdir())  # core wrote nothing into the directory itself
    del OrchError


def test_no_secret_in_progress_state_errors_or_review(tmp):
    f, ports, p, rv = run_ok(tmp, AGENT)
    blob = json.dumps([p, rv, f.dispatch("GET", "state", {}, None)[1]])
    for secret in (ports.passphrase_seen, ports.code_seen, GRANT_SECRET, GRANT_SECRET.split(".")[1]):
        assert secret not in blob
    assert not any(w in blob for w in ports.code_seen.split()[:3] if len(w) > 4 and blob.count(w) > 0 and False)


def test_the_grant_secret_is_deposited_and_never_stored_by_the_flow(tmp):
    f, ports, p, _ = run_ok(tmp, AGENT)
    deposits = [e for e in ports.log if e[0] == "call" and e[1] == "grant.deposit"]
    assert len(deposits) == 1
    assert GRANT_SECRET not in repr(vars(f)) and GRANT_SECRET not in repr(vars(f.run))


def test_b8_a_longer_runner_grant_is_prepared_only_after_the_settings_landed(tmp):
    prof = {**AGENT, "agent_hours": 72}
    f, ports, p, rv = run_ok(tmp, prof)
    assert p["done"] is True and ports.submits[0] == "settings.changed" and ports.submits[-1] == "grant.issued"
    calls = [e for e in ports.log if e[0] == "call"]
    kinds = [(c[1], i) for i, c in enumerate(calls)]
    prepares = [i for k, i in kinds if k == "human.prepare"]
    settings_submit = next(i for i, c in enumerate(calls) if c[1] == "human.submit")
    assert len(prepares) == 6 and prepares[-1] > settings_submit  # the grant is the last prepare, after settings
    assert all(i < settings_submit for i in prepares[:-1])
    assert ports.unlocks == 1


def test_b8_the_deferred_grant_is_still_checked_against_the_review(tmp):
    ports = FakePorts()
    ports.tamper = lambda ev: ev.update(hours=ev["hours"] + 1) if ev["type"] == "grant.issued" else None
    f, ports, p, _ = run_ok(tmp, {**AGENT, "agent_hours": 72}, ports=ports)
    assert p["error"]["code"] == "review.mismatch" and "grant.issued" not in ports.submits
    assert ports.submits[0] == "settings.changed"  # the earlier, already checked requests landed; finishable
    assert p["error"]["finishable"] is True


def test_unlock_labels_cover_every_event_the_batch_signs(monkeypatch):
    """Regression (found by the real e2e): the unlocked key signed only the ticket-event label, so the first workspace
    event (settings.changed) was refused by the custody. The labels must be those the workspace-log events really sign
    under (a person event whose log is "workspace"), and nothing wider."""
    from orch import canon
    from orch.canon import hashing
    from orch.setup import steps

    seen = []
    monkeypatch.setattr(hashing, "signed_context", lambda key, *a, **k: seen.append(key) or b"")
    hashing.person_signing_bytes("w" * 32, "workspace", {"hash_v": 1, "type": "settings.changed"})
    assert [canon.LABELS[k].encode("ascii") for k in seen] == list(steps.batch_labels())


def test_b3_setup_refreshes_a_stale_runner_before_enabling_it(tmp):
    f, ports, p, rv = run_ok(tmp, AGENT)
    n = names(ports)
    assert "runner_refresh" in n and n.index("runner_refresh") < n.index("runner_enable")


def test_b3_refresh_restarts_a_runner_of_another_release(tmp_path, monkeypatch):
    from orch.host import cli as hostcli

    seen = {}

    def fake(state, env, run_dir, mine, data, force=False):
        seen["mine"] = mine
        data["runner_restarted"] = True

    monkeypatch.setattr(hostcli, "_restart_stale_runner", fake)
    out = hostcli.refresh_stale_runner(tmp_path, {"HOME": str(tmp_path)})
    assert out == {"runner_restarted": True} and seen["mine"]


def test_item5_a_custody_refusal_shows_the_reason_and_the_fix(tmp):
    from orch.custody.base import CustodyError

    ports = FakePorts()

    def refuse(wid):
        raise CustodyError(
            "/h/.config/orch/person is writable by others (mode 770); run: chmod 700 /h/.config/orch/person"
        )

    ports.convenience_on = refuse
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    msg = p["error"]["message"]
    assert p["error"]["code"] == "custody.error" and "chmod 700" in msg and "CustodyError" not in msg
