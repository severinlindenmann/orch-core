"""H5c click-to-sign (orch-v2-host §0.8.6): the per-workspace flag and readers condition at the host, the click signer
against a served workspace, and the signer's guarantees. The device key lives in a FakeStore: never the login
keychain."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from orch import crypto
from orch.click import view as views
from orch.click.protocol import RISK, ClickError, op_class
from orch.click.signer import Budget, ClickSigner
from orch.custody.convenience import ConvenienceBackend, FakeStore
from orch.custody.select import Selection
from orch.host import convenience
from orch.host.client import Connection, Pin
from orch.host.control_client import new_idem
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_human import logged

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Click:
    """A click signer over the served workspace, with the owner's device key in a FakeStore."""

    def __init__(self, served, *, enable: bool = True) -> None:
        self.served = served
        owner = served.lab.ws.owner
        self.owner = owner
        self.store = FakeStore()
        self.store.put(served.wid, owner.device, crypto.private_scalar(owner.dk_sig))
        self.gen = 1
        self.backends: list[ConvenienceBackend] = []
        self.entry = {
            "workspace_id": served.wid,
            "prefix": served.service.entry.prefix,
            "root": served.lab.root,
            "host_uid": UID,
            "peers": {str(UID): {"class": "person", "person": owner.ref}},
        }
        self.mono = Mono()
        self.conns: list[Connection] = []
        self.signer = ClickSigner(
            served.lab.state,
            uid=UID,
            connect=self._connect,
            custody=self._custody,
            workspaces=lambda: [self.entry],
            enabled=lambda e: convenience.enabled(served.lab.state, served.wid, UID),
            mono=self.mono,
            wall=lambda: served.lab.ws.clock[0],
        )
        if enable:
            convenience.set_enabled(served.lab.state, served.wid, True, UID)

    def _connect(self, entry):
        c = Connection(Pin(UID, entry["workspace_id"], self.served.lab.run / entry["workspace_id"] / "host.sock"))
        self.conns.append(c)
        return c

    def _custody(self, state, wid):
        sel = Selection(
            {
                "v": 1,
                "custody": "convenience",
                "device": self.owner.device,
                "pub": crypto.b64u(self.owner.sig_pub),
                "gen": self.gen,
                "transition": None,
            }
        )
        b = ConvenienceBackend(self.store, wid, self.owner.device, self.owner.sig_pub)
        self.backends.append(b)
        return b, sel

    @property
    def signatures(self) -> int:
        return sum(b.signatures for b in self.backends)

    def prepare(self, op: str, args: dict, **kw):
        return self.signer.prepare({"workspace_id": self.served.wid, "op": op, "args": args, "idem": new_idem(), **kw})

    def view(self, rid: str):
        return self.signer.view({"workspace_id": self.served.wid, "request_id": rid})

    def sign(self, rid: str, digest: str, **kw):
        return self.signer.sign({"workspace_id": self.served.wid, "request_id": rid, "view_digest": digest, **kw})

    def close(self) -> None:
        self.signer.close()


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def refused(fn, code: str, reason: str | None = None) -> ClickError:
    with pytest.raises(ClickError) as info:
        fn()
    assert info.value.code == code, info.value
    if reason is not None:
        assert info.value.reason == reason, info.value
    return info.value


def pending(served, rid: str) -> dict:
    (item,) = ok(served.client().req("human.pending", {"request_id": rid}))["data"]["requests"]
    return item


def add_device(lab) -> str:
    store = lab.ws.other()
    lab.ws.store = store
    try:
        ref, _ = lab.ws.add_device()
    finally:
        store.close()
        lab.ws.store = None
    return ref


# --- the flag and the readers condition (owner decision, §0.8.6 item 1) ---------------------------------------------


def test_the_flag_is_off_by_default_and_refuses_convenience_at_prepare(served, human):
    c = Click(served, enable=False)
    try:
        _, key = human.ticket()
        e = refused(lambda: c.prepare("log", {"ref": key, "text": "x"}), "sign.convenience_unavailable", "disabled")
        assert "disabled" in e.message
        # the host refuses a convenience signer at prepare whatever the client is
        signer = {"device": human.person.device, "auth": "convenience"}
        res = human.prepare_res("log", {"ref": key, "text": "x"}, signer=signer)
        assert err(res) == "sign.convenience_unavailable"
        # orch sign stays: a passphrase signer is not affected
        ok(human.prepare_res("log", {"ref": key, "text": "x"}))
    finally:
        c.close()


@pytest.mark.parametrize("why", ["relay_paired", "other_member", "phone_device", "reader_version"])
def test_each_readers_reason_refuses_at_prepare(click, served, human, monkeypatch, why):
    _, key = human.ticket()
    if why == "relay_paired":
        (served.lab.state / "hosts" / served.wid / "membership").mkdir(parents=True)
    elif why == "other_member":
        served.lab.add_member()
    elif why == "phone_device":
        add_device(served.lab)
    else:
        monkeypatch.setattr(convenience, "_model_knows", lambda: False)
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "sign.convenience_unavailable", why)


def test_the_condition_is_checked_again_at_binding_and_at_submit(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    click.mono.t += 2
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "sign.convenience_unavailable", "disabled")
    # a hand-off request bound with a convenience signer is refused at human.get too
    h = human.prepare("log", {"ref": key, "text": "y"}, handoff=True)
    res = human.get(h["request"]["id"], signer={"device": human.person.device, "auth": "convenience"})
    assert err(res) == "sign.convenience_unavailable"
    assert pending(served, h["request"]["id"])["state"] == "prepared"  # not bound


def test_member_add_is_refused_while_the_flag_is_on(click, served, human):
    from tests.identity.helpers import Person

    p = Person()
    args = {"name": "q", "pk": crypto.b64u(p.pk_pub), "cert": "cert.json"}
    files = {"cert": {"b64": __import__("base64").b64encode(json.dumps(p.cert()).encode()).decode()}}
    res = human.prepare_res("member.add", args, handoff=True, files=files)
    assert err(res) == "sign.convenience_readers"


def test_member_add_is_refused_with_convenience_events_in_the_history(click, served, human):
    from tests.identity.helpers import Person

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "via a click"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    click.sign(p["request_id"], v["view_digest"])
    convenience.set_enabled(served.lab.state, served.wid, False, UID)  # switching off does not help now
    q = Person()
    args = {"name": "q", "pk": crypto.b64u(q.pk_pub), "cert": "cert.json"}
    files = {"cert": {"b64": __import__("base64").b64encode(json.dumps(q.cert()).encode()).decode()}}
    res = human.prepare_res("member.add", args, handoff=True, files=files)
    assert err(res) == "sign.convenience_readers"


def test_member_add_works_after_switching_off_when_there_are_no_convenience_events(click, served, human):
    from tests.identity.helpers import Person

    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    q = Person()
    args = {"name": "q", "pk": crypto.b64u(q.pk_pub), "cert": "cert.json"}
    files = {"cert": {"b64": __import__("base64").b64encode(json.dumps(q.cert()).encode()).decode()}}
    ok(human.prepare_res("member.add", args, handoff=True, files=files))


# --- the click signer -------------------------------------------------------------------------------------------------


def test_a_click_is_signed_end_to_end_with_auth_convenience(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "via a click"})
    assert p["confirm"] == "click"
    assert pending(served, p["request_id"])["code"] is None  # bound at prepare: no hand-off code
    v = click.view(p["request_id"])
    view = v["view"]
    assert view["risk"] == RISK
    assert view["auth"] == "convenience" and view["confirm"] == {"kind": "click"}
    assert v["view_digest"] == views.digest(view)
    # the lines are the terminal review of orch sign, line for line
    from orch.host import signer as host_signer

    prepared = ok(human.get(p["request_id"]))["data"]
    names = lambda w: (served.lab.ws.root.name, served.service.entry.prefix) if w == served.wid else None  # noqa: E731
    assert view["lines"] == host_signer.render(prepared, names).rstrip("\n").split("\n")
    click.mono.t += 1.5
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["duplicate"] is False and res["result"]["key"] == key
    last = logged(served, key)[-1]
    assert last["type"] == "log.added" and last["text"] == "via a click" and last["auth"] == "convenience"
    assert click.signatures == 1
    # a repeated sign answers from the cache and signs nothing new
    assert click.sign(p["request_id"], v["view_digest"])["duplicate"] is True
    assert click.signatures == 1


def test_sign_refusals_sign_nothing_and_do_not_cancel(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    rid = p["request_id"]
    refused(lambda: click.sign(rid, "sha256:" + "0" * 64), "human.click_refused", "view_changed")  # no view yet
    v = click.view(rid)
    refused(lambda: click.sign(rid, v["view_digest"]), "human.click_refused", "too_soon")
    click.mono.t += 2
    refused(lambda: click.sign(rid, "sha256:" + "1" * 64), "human.click_refused", "view_changed")
    click.gen += 1
    refused(lambda: click.sign(rid, v["view_digest"]), "human.click_refused", "custody_changed")
    assert click.signatures == 0
    assert pending(served, rid)["state"] == "bound"  # refusals never cancel the host request


def test_view_and_sign_refuse_a_request_the_signer_did_not_prepare(click, served, human):
    _, key = human.ticket()
    h = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    rid = h["request"]["id"]
    refused(lambda: click.view(rid), "human.click_refused", "not_prepared_here")
    refused(lambda: click.sign(rid, "sha256:" + "0" * 64), "human.click_refused", "not_prepared_here")
    assert pending(served, rid)["state"] == "prepared"  # untouched: still the terminal's


def test_terminal_only_ops_are_refused_before_the_host_is_asked(click, served, human):
    for op in ("grant", "frobnicate", "init", "member.rename"):
        refused(lambda op=op: click.prepare(op, {}), "human.click_refused", "terminal_only")
    assert click.conns == []


def test_an_expired_approval_is_refused(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 11 * 60
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "expired")


def test_cancel_cancels_and_forgets_the_approval(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    out = click.signer.cancel({"workspace_id": served.wid, "request_id": p["request_id"]})
    assert out["state"] == "cancelled"
    refused(lambda: click.view(p["request_id"]), "human.click_refused", "not_prepared_here")


def test_a_lost_submit_answer_is_retried_with_the_identical_events_and_idem(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    sent: list = []

    def lossy_connect(entry):
        conn = click._connect(entry)
        real = conn.call

        def lossy(op, args, **kw):
            out = real(op, args, **kw)
            if op == "human.submit":
                sent.append((json.dumps(args, sort_keys=True), kw.get("idem")))
                if len(sent) <= 2:
                    raise ConnectionError("lost")  # committed, but the answer never arrives (twice)
            return out

        conn.call = lossy
        return conn

    click.signer._connect = lossy_connect
    click.signer.close()  # the next call connects through lossy_connect
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "uncertain")
    res = click.sign(p["request_id"], v["view_digest"])  # the retry resends, never re-signs
    assert res["result"]["key"] == key and res["duplicate"] is True
    assert click.signatures == 1
    assert len({s for s in sent}) == 1  # identical events and idem every time
    assert [e["text"] for e in logged(served, key) if e["type"] == "log.added"] == ["x"]


def test_view_digest_ignores_the_check_times_only():
    v = {"risk": RISK, "lines": ["a"], "checks": [{"repo": "r", "clean": True, "attested_by": "worker", "at": "1"}]}
    w = json.loads(json.dumps(v))
    w["checks"][0]["at"] = "2"
    assert views.digest(v) == views.digest(w)
    w["checks"][0]["repo"] = "other"
    assert views.digest(v) != views.digest(w)
    w = json.loads(json.dumps(v))
    w["lines"] = ["b"]
    assert views.digest(v) != views.digest(w)


def test_op_classes_fail_closed_and_repo_settings_need_the_word():
    assert op_class("approve") == "click" and op_class("member.remove") == "word" and op_class("policy.set") == "word"
    assert op_class("grant") == "terminal" and op_class("some.future.op") == "terminal"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"grant_hours": 4}}]) == "click"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"repos": {}}}]) == "word"
    assert op_class("settings.set", [{"type": "settings.changed", "set": "odd"}]) == "word"


def test_workspace_scope_words_carry_the_prefix_and_the_workspace_fragment():
    wid = "0123456789abcdef0123456789abcdef"
    names = lambda w: ("acme", "DEMO") if w == wid else None  # noqa: E731
    base = {"workspace_id": wid, "material": {}, "events": [{"name": "lint"}]}
    assert views.word({**base, "op": "policy.set"}, names) == "DEMO~89abcdef/POLICY"
    assert views.word({**base, "op": "settings.set"}, names) == "DEMO~89abcdef/SETTINGS"
    assert views.word({**base, "op": "addon.grant"}, names) == "lint~89abcdef"


def test_the_budget_counts_event_signatures_on_monotonic_time():
    t = Mono()
    b = Budget(t, per_minute=30, per_hour=600)
    b.reserve(16)
    b.reserve(14)
    with pytest.raises(ClickError) as info:
        b.reserve(1)
    assert info.value.code == "rate.limited" and 0 < info.value.retry_after_ms <= 60_000
    t.t += 61
    b.reserve(30)
    for _ in range(18):
        t.t += 61
        b.reserve(30)
    t.t += 61
    with pytest.raises(ClickError):  # 600 in the hour
        b.reserve(30)


def test_status_reads_metadata_without_loading_a_key(click, served):
    out = click.signer.status({})
    assert out["contract"] == 1 and out["core"]
    (row,) = out["workspaces"]
    assert row["workspace_id"] == served.wid and row["enabled"] is True
    assert click.store.gets == 0


def test_a_host_with_another_op_contract_is_refused(click, served, human, monkeypatch):
    real = ClickSigner._check_welcome
    welcome = dict(click._connect(click.entry).welcome)
    real(welcome)  # the served host's welcome passes
    welcome["registry"] = "sha256:" + "0" * 64
    with pytest.raises(ClickError) as info:
        real(welcome)
    assert info.value.code == "host.version_mismatch"


# --- the process boundary -----------------------------------------------------------------------------------------


def test_the_dashboard_side_never_imports_custody_code():
    code = textwrap.dedent(
        """
        import sys
        import orch.click.spawn, orch.click.client
        bad = [m for m in sys.modules if m.startswith(("orch.custody", "orch.click.signer", "orch.click.view"))]
        print(bad)
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_the_real_child_answers_status_and_exits_when_the_channel_closes(tmp_path):
    from orch.click import spawn

    env = {"ORCH_STATE_DIR": str(tmp_path / "state"), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    client = spawn.start(env)
    try:
        out = client.call("status", {}, timeout=60)
        assert out["contract"] == 1 and out["workspaces"] == []
        with pytest.raises(ClickError) as info:
            client.call("view", {"workspace_id": "0" * 32, "request_id": "hr_" + "0" * 26}, timeout=60)
        assert info.value.code == "not_found"
    finally:
        client.close()
    assert client.proc.returncode is not None


def test_an_agent_cannot_start_the_signer(tmp_path):
    import os
    import socket

    a, b = socket.socketpair()
    try:
        env = {**os.environ, "ORCH_GRANT": "x", "ORCH_STATE_DIR": str(tmp_path)}
        n = b.fileno()
        p = subprocess.run(
            [sys.executable, "-I", "-m", "orch.click", "--fd", str(n)], pass_fds=(n,), env=env, capture_output=True
        )
        assert p.returncode == 3
    finally:
        a.close()
        b.close()
