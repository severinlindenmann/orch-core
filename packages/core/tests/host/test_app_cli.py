"""#1455: ``orch app`` (the CLI): who may run what, the order of signing and sending, and that nothing is called before
the shape is right. The host half is ``tests/host/test_appops.py``; the host is replaced here."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import app

STAGED = "pa_" + "1" * 32


def run(argv, env, *, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    code = app.main([*argv, *(["--json"] if as_json else [])], env=env, out=out, err=err)
    return code, out.getvalue()


def never(*a, **k):
    raise AssertionError("the host must not be reached")


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(app, "_target", never)
    monkeypatch.setattr(app, "_call", never)
    monkeypatch.setattr(app, "_sign", never)


@pytest.mark.parametrize("argv", [["release", STAGED], ["send", STAGED]])
def test_an_agent_can_neither_release_nor_send(offline, argv):
    code, out = run(argv, {"ORCH_GRANT": "gr_x", "ORCH_SESSION": "s.1"})
    assert code == 3 and out.startswith("error op.not_allowed: a person does this; an agent only prepares")


def test_an_agent_refusal_in_json(offline):
    code, out = run(["release", STAGED], {"ORCH_GRANT": "gr_x"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed"


@pytest.mark.parametrize(
    "argv",
    [[], ["bogus"], ["prepare"], ["prepare", "TST-0001"], ["prepare", "TST-0001", "a.html"],
     ["prepare", "TST-0001", "a.html", "--name", "N", "--kind", "app"], ["release"], ["release", "a", "b"],
     ["send"], ["discard"], ["list", "a", "b"],
     ["prepare", "T", "a", "--name", "N", "--kind", "x", "--access", "public"]],
)  # fmt: skip
def test_a_malformed_command_prints_the_usage_and_touches_nothing(offline, argv):
    code, out = run(argv, {})
    assert code == 2 and "usage: orch app" in out


class Rec:
    def __init__(self):
        self.calls = []

    def target(self, env):
        return SimpleNamespace()

    def call(self, target, op, args, env):
        self.calls.append((op, args))
        if op == "app.send":
            return {"app_id": "a" * 32, "version": 1, "uploaded": True, "live": False, "needs": "the server's approval"}
        if op == "app.prepare":
            return {"staged_id": STAGED, "ticket": "TST-0001", "files": [{}, {}], "access": args["access"]}
        return {}

    def sign(self, target, op, args, capture=None):
        self.calls.append(("sign:" + op, args))
        if capture is not None:
            capture.append({"events": [{"type": "log.added", "text": "App approval: let an app go live\nx"}]})
        return {}


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(app, "_target", r.target)
    monkeypatch.setattr(app, "_call", r.call)
    monkeypatch.setattr(app, "_sign", r.sign)
    monkeypatch.setattr(app, "_tty_ok", lambda: True)
    return r


def test_release_signs_first_then_sends_and_says_it_is_not_live(rec):
    code, out = run(["release", STAGED], {})
    assert code == 0 and [c[0] for c in rec.calls] == ["sign:app.release", "app.send"]
    assert rec.calls[0][1] == {"staged_id": STAGED}
    assert "not live yet" in out


def test_release_without_a_terminal_stops_before_anything_is_signed(rec, monkeypatch):
    monkeypatch.setattr(app, "_tty_ok", lambda: False)
    code, out = run(["release", STAGED], {})
    assert code == 3 and "no terminal" in out and rec.calls == []


def test_a_refused_signature_sends_nothing(rec, monkeypatch):
    def refused(target, op, args, capture=None):
        raise app.PubError("invalid.input", "not confirmed: nothing was signed", 5)

    monkeypatch.setattr(app, "_sign", refused)
    code, out = run(["release", STAGED], {})
    assert code == 5 and rec.calls == [] and "nothing was signed" in out


def test_an_agent_prepare_passes_the_choices_and_says_nothing_is_live(rec):
    code, out = run(
        ["prepare", "TST-0001", "index.html", "app.js", "--name", "Board", "--kind", "app", "--access", "company",
         "--reach", "https://api.example.com", "--slug", "board"],
        {"ORCH_GRANT": "gr_x", "ORCH_SESSION": "s.1"},
    )  # fmt: skip
    assert code == 0 and rec.calls == [
        ("app.prepare", {"ref": "TST-0001", "artifacts": ["index.html", "app.js"], "name": "Board", "kind": "app",
                         "access": "company", "slug": "board", "reach": ["https://api.example.com"]})
    ]  # fmt: skip
    assert "nothing is live" in out and f"orch app release {STAGED}" in out


# -- the second signature of `orch app release` -----------------------------------------------------------------


def _commit_for(person, *, host="pub.example.org", hub="cd" * 16, ws="ab" * 16, **over):
    """A commit request as the host draws it, and the note text the person signed, for the first version of an app."""
    from orch import crypto
    from orch.publishclient import approval, golive
    from tests.publishclient.test_app_release import SERVER, SHARE, make_meta

    meta = make_meta(access="company", audience="company")
    facts = {
        "op": "commit", "app_id": "ef" * 16, "version": 1, "meta": meta, "previous": None, "staged_id": STAGED,
        "ticket_key": "TST-0001", "origin": "agent", "server": SERVER, "share": SHARE,
    }  # fmt: skip
    keys = ("staged_id", "ticket_key", "origin", "op", "app_id", "version", "meta", "previous", "server", "share")
    note = approval.app_release_text(**{k: facts[k] for k in keys})
    from orch.wsproto.pubsign import PublishContext

    host = PublishContext(ws, SERVER).host
    body = golive.commit_body(meta, "commit", facts["app_id"], 1)
    commit = {
        "request_id": "cr_" + "0" * 32, "op": "commit", "app_id": facts["app_id"], "version": 1, "method": "POST",
        "host": host, "target": golive.commit_target(ws, facts["app_id"], "commit"), "body_hex": body.hex(),
        "ts_ms": 1_790_000_000_000, "nonce_hex": "07" * 16, "hub_id": hub, "facts": facts,
        "pk_pub": crypto.b64u(person.pk_pub), "device_certs": [person.cert()],
    }  # fmt: skip
    commit.update(over)
    return commit, note, ws


class _Backend:
    auth = "key"

    def __init__(self, person):
        self.person, self.signed = person, []

    def public_key(self, key_id):
        return self.person.sig_pub

    def sign(self, key_id, payload, *, action):
        self.signed.append((payload, action))
        return self.person.dk_sign(payload)


def _approve_with(monkeypatch, commit, note, ws, person, now_ms=1_790_000_000_000, local_hub="cd" * 16):
    from orch.ops import human

    backend = _Backend(person)
    monkeypatch.setattr(human, "device_backend", lambda state, wid, convenience=False: backend)
    monkeypatch.setattr(app, "_local_hub_id", lambda target: local_hub)
    target = SimpleNamespace(workspace_id=ws, state=None)
    return app._approve(target, commit, note, now_ms), backend  # noqa: SLF001


def test_the_client_approves_only_what_the_note_it_signed_says(monkeypatch):
    from orch.canon.jcs import dumps as cj
    from orch.crypto import suite as S
    from orch.crypto.encoding import unb64u
    from orch.crypto.labels import L
    from tests.identity.helpers import Person

    person = Person()
    commit, note, ws = _commit_for(person)
    ap, backend = _approve_with(monkeypatch, commit, note, ws, person)
    assert S.verify(person.sig_pub, unb64u(ap["sig"], 64), L["sig_hub_publish_approval"] + cj(ap["o"]))
    assert len(backend.signed) == 1 and backend.signed[0][1] == "let an app go live"
    # every deviation is refused and NOTHING is signed
    for tamper in (
        {"target": commit["target"].replace("commit", "start")}, {"host": "evil.example.org"},
        {"body_hex": commit["body_hex"][:-2] + "00"}, {"ts_ms": commit["ts_ms"] + 10**6},
    ):  # fmt: skip
        with pytest.raises(app.PubError) as caught:
            _approve_with(monkeypatch, {**commit, **tamper}, note, ws, person)
        assert caught.value.code == "publish.changed", tamper
    with pytest.raises(app.PubError):
        _approve_with(monkeypatch, commit, note + " ", ws, person)  # not the note that was signed
    swapped = {**commit, "facts": {**commit["facts"], "meta": {**commit["facts"]["meta"], "name": "Other"}}}
    with pytest.raises(app.PubError):
        _approve_with(monkeypatch, swapped, note, ws, person)  # a host that swapped the release has other words
    other = Person()
    with pytest.raises(app.PubError):  # no certificate for this key in the list the host gave
        _approve_with(monkeypatch, {**commit, "device_certs": [other.cert()]}, note, ws, person)


def test_release_makes_it_live_when_the_host_draws_a_request(rec, monkeypatch):
    commit_doc = {"request_id": "cr_" + "1" * 32, "marker": 1}
    seen = {}

    def call(target, op, args, env):
        rec.calls.append((op, args))
        if op == "app.send":
            return {"app_id": "a" * 32, "version": 1, "uploaded": True, "live": False, "commit": commit_doc}
        if op == "app.commit":
            seen["args"] = args
            return {"app_id": "a" * 32, "version": 1, "op": "commit", "live": True}
        return {}

    monkeypatch.setattr(app, "_call", call)
    monkeypatch.setattr(app, "_approve", lambda target, commit, note, now: {"signed": note, "for": commit["marker"]})
    code, out = run(["release", STAGED], {})
    assert code == 0 and [c[0] for c in rec.calls] == ["sign:app.release", "app.send", "app.commit"]
    assert seen["args"] == {
        "request_id": commit_doc["request_id"],
        "approval": {"signed": "App approval: let an app go live\nx", "for": 1},
    }
    assert "ok app live" in out


def test_a_host_that_names_another_hub_gets_no_signature(monkeypatch):
    """The hub a signature is bound to is this computer's own (``hub.json``), never the host's answer."""
    from tests.identity.helpers import Person

    person = Person()
    commit, note, ws = _commit_for(person)
    with pytest.raises(app.PubError):  # the host says hub 00..., this computer's join state says cd...
        _approve_with(monkeypatch, {**commit, "hub_id": "00" * 16}, note, ws, person)
    with pytest.raises(app.PubError):  # a computer with no hub signs nothing
        _approve_with(monkeypatch, commit, note, ws, person, local_hub=None)
    # and when the join state is the host's claim, it is accepted: only agreement signs
    _approve_with(monkeypatch, {**commit, "hub_id": "ab" * 16}, note, ws, person, local_hub="ab" * 16)


def test_the_local_hub_is_read_from_the_join_state_and_unreadable_means_none(tmp_path):
    import json
    import os

    target = SimpleNamespace(state=tmp_path, workspace_id="ab" * 16)
    assert app._local_hub_id(target) is None  # noqa: SLF001 - no join state at all
    d = tmp_path / "hosts" / target.workspace_id / "hub"
    d.mkdir(parents=True, mode=0o700)
    (d / "hub.json").write_text("{not json")
    os.chmod(d / "hub.json", 0o600)
    assert app._local_hub_id(target) is None  # noqa: SLF001 - unreadable: fail closed
    (d / "hub.json").write_text(json.dumps({}))
    assert app._local_hub_id(target) is None  # noqa: SLF001 - not a bound hub state


def test_a_commit_that_fails_is_tried_again_with_a_fresh_request_twice_then_the_right_next_step_is_said(
    rec, monkeypatch
):
    """N2: the stage is gone after the upload: the retry is ``commit_request``, never "send it again"."""
    from orch.ops.errors import OrchError

    state = {"commits": 0, "requests": 0}

    def call(target, op, args, env):
        rec.calls.append((op, args))
        if op == "app.send":
            first = {"request_id": "cr_" + "1" * 32, "app_id": "a" * 32, "op": "commit", "version": 1}
            return {"app_id": "a" * 32, "version": 1, "uploaded": True, "live": False, "commit": first}
        if op == "app.commit_request":
            state["requests"] += 1
            return {"request_id": "cr_" + str(state["requests"]) * 32, "app_id": "a" * 32, "op": "commit", "version": 1}
        if op == "app.commit":
            state["commits"] += 1
            if state["commits"] < state["fail"]:
                raise OrchError("publish.changed", "that request is gone or too old: try again")
            return {"app_id": "a" * 32, "version": 1, "op": "commit", "live": True}
        return {}

    monkeypatch.setattr(app, "_call", call)
    monkeypatch.setattr(app, "_approve", lambda target, commit, note, now: {"for": commit["request_id"]})
    state["fail"] = 3  # the first two commits fail, the third (the second retry) goes through
    code, out = run(["release", STAGED], {})
    assert code == 0 and state["requests"] == 2 and "ok app live" in out
    ops = [c[0] for c in rec.calls]
    assert ops.count("app.commit_request") == 2 and ops.count("app.send") == 1  # the upload is never repeated
    state.update(commits=0, requests=0, fail=99)  # always failing: two retries, then the right advice
    rec.calls.clear()
    code, out = run(["release", STAGED], {})
    assert code != 0 and state["requests"] == 2
    assert "NOT live" in out and "orch app send" not in out and "prepare the same release again" in out
