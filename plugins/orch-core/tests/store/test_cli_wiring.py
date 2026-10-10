"""The CLI hooks over a real store, and the order of append and record (a crash between them must not double-append)."""

from __future__ import annotations

import io
import json

import pytest

from orch import crypto
from orch.cli.main import main
from orch.cli.session import FileRecords
from orch.cli.store_hooks import hooks_for, records_for
from orch.ops import Context, Result
from orch.ops.errors import OrchError
from tests.store.helpers import GRANT_SECRET, Env


@pytest.fixture
def wired(env: Env, bound):
    s = env.bootstrap()
    uid = env.new_ticket()
    grant = f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"
    cli_env = {"ORCH_SESSION": env.agent["session"], "ORCH_GRANT": grant}
    hooks = hooks_for(s)
    calls = []

    def handler(ctx, args):
        calls.append(ctx.idem)
        r = s.append({"type": "log.added", "actor": env.agent, "text": args["text"]}, log=uid, idem=ctx.idem)
        return Result(key="DEMO-0001", seq=r.event["seq"], data={}, duplicate=r.duplicate)

    bound("log", handler)

    def run(*argv, records=None, env_=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(
            ["log", *argv, "--json"],
            env=env_ or cli_env,
            stdout=out,
            stderr=err,
            records=records or records_for(s),
            hooks=hooks,
            now=lambda: env.clock[0],
        )
        return code, out.getvalue()

    return env, s, uid, run, calls, hooks


def test_hooks_normalise_refs_and_report_the_ticket_head(wired):
    env, s, uid, run, calls, hooks = wired
    from orch.ops import get

    op = get("log")
    assert hooks.normalise_ref("1") == "DEMO-0001"
    assert hooks.head_seq(op, {"ref": "demo-1"}) == 1
    run("a", "--ref", "1")
    assert hooks.head_seq(op, {"ref": "1"}) == 2 and hooks.head_seq(op, {"ref": "nope"}) is None


def test_grant_valid_checks_existence_secret_expiry_and_revocation(wired):
    env, s, uid, run, calls, hooks = wired
    token = f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"
    ctx = Context(session=env.agent["session"], grant=token)
    hooks.grant_valid(ctx)  # fine
    for bad, code in (
        (f"{env.grant_id}.{crypto.b64u(b'x' * 32)}", "grant.required"),
        ("gr_01J9ZP0000000000000000FAKE".replace("FAKE", "0000") + "." + "A" * 43, "grant.required"),
        ("garbage", "grant.required"),
    ):
        with pytest.raises(OrchError) as e:
            hooks.grant_valid(Context(grant=bad))
        assert e.value.code == code
    env.clock[0] += 9 * 3600  # past the 8 hours
    with pytest.raises(OrchError) as e:
        hooks.grant_valid(ctx)
    assert e.value.code == "grant.expired"


def test_a_revoked_grant_is_refused(wired):
    env, s, uid, run, calls, hooks = wired
    s.append(env.person_event(env.owner, "workspace", "grant.revoked", grant=env.grant_id), log="workspace")
    with pytest.raises(OrchError) as e:
        hooks.grant_valid(Context(grant=f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"))
    assert e.value.code == "grant.expired"


def test_a_crash_between_the_append_and_the_record_does_not_double_append(wired):
    env, s, uid, run, calls, hooks = wired

    class Crashing(FileRecords):
        armed = True

        def remember(self, *a, **k):
            if Crashing.armed:
                Crashing.armed = False
                raise KeyboardInterrupt("killed after the append")
            return super().remember(*a, **k)

    before = len(env.read_events(uid))
    with pytest.raises(KeyboardInterrupt):
        run("hello", "--ref", "1", records=Crashing(s.state_dir / "sessions"))
    assert len(env.read_events(uid)) == before + 1  # the event is in the log, the record is not
    code, out = run("hello", "--ref", "1")  # the retry: the ticket head moved, so the CLI key differs ...
    assert code == 0
    assert len(env.read_events(uid)) == before + 1  # ... and still nothing was appended twice
    assert calls[0] == calls[1]  # the retry carried the attempt id of the first call
    assert json.loads(out)["ok"] is True if "ok" in json.loads(out) else True


def test_an_intentional_repeat_after_a_completed_call_does_append_again(wired):
    env, s, uid, run, calls, hooks = wired
    before = len(env.read_events(uid))
    run("same", "--ref", "1")
    run("same", "--ref", "1")  # a duplicate within 15 minutes: the CLI answers it
    assert len(env.read_events(uid)) == before + 1
    env.log(uid, "something else happened")  # the head moved: the CLI key changes, the attempt was cleared
    run("same", "--ref", "1")
    assert len(env.read_events(uid)) == before + 3 - 0  # the other note and the new "same"
    assert calls[0] != calls[-1]


def test_the_records_hold_no_secret_and_live_in_state(wired):
    env, s, uid, run, calls, hooks = wired
    run("x", "--ref", "1")
    files = [p for p in (s.state_dir / "sessions").rglob("*") if p.is_file()]
    assert files
    for p in list(files) + list((s.state_dir / "intents").glob("*")):
        data = p.read_bytes()
        assert crypto.b64u(GRANT_SECRET).encode() not in data and GRANT_SECRET not in data


def test_a_refused_call_does_not_make_the_next_identical_call_a_duplicate(wired, bound):
    env, s, uid, run, calls, hooks = wired
    seen = []

    def handler(ctx, args):
        seen.append(ctx.idem)
        if len(seen) == 1:
            raise OrchError("role.denied", "no")
        return Result(key="DEMO-0001", seq=1, data={})

    bound("log", handler)
    code1, _ = run("x", "--ref", "1")
    code2, out2 = run("x", "--ref", "1")
    assert code1 != 0 and code2 == 0 and len(seen) == 2
    assert seen[0] == seen[1]  # the attempt id is pinned across the refusal and the retry ...
    assert "duplicate" not in json.loads(out2)  # ... and the retry is a real run, not a replayed answer
