"""H5c click-to-sign (orch-v2-host §0.8.6): the per-workspace flag and readers condition at the host, the click signer
against a served workspace, and the signer's guarantees. The device key lives in a FakeStore: never the login
keychain."""

from __future__ import annotations

import copy
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
from tests.conftest import child_env
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

    #: ``tamper(op, args, result) -> result`` rewrites the host's answers (a lying or changing host)
    tamper = None

    def _connect(self, entry):
        c = Connection(Pin(UID, entry["workspace_id"], self.served.lab.run / entry["workspace_id"] / "host.sock"))
        real = c.call

        def call(op, args, **kw):
            res = real(op, args, **kw)
            return self.tamper(op, args, copy.deepcopy(res)) if self.tamper is not None else res

        c.call = call
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
    e = refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "sign.convenience_unavailable", why)
    # #517: the host's detail reaches the dashboard over the real socket, re-validated
    assert e.detail is not None and e.detail["reason"] == why
    assert e.error()["detail"] == e.detail


def test_the_condition_is_checked_again_at_binding_and_at_submit(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    click.mono.t += 2
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "sign.convenience_unavailable", "disabled")
    # a hand-off request bound with a convenience signer is refused at human.get too
    h = human.prepare_unbound("log", {"ref": key, "text": "y"})
    res = human.get(h["request"]["id"], signer={"device": human.person.device, "auth": "convenience"})
    assert err(res) == "sign.convenience_unavailable"
    assert pending(served, h["request"]["id"])["state"] == "prepared"  # not bound


def test_member_add_is_refused_while_the_flag_is_on(click, served, human):
    from tests.identity.helpers import Person

    p = Person()
    args = {"name": "q", "pk": crypto.b64u(p.pk_pub), "cert": "cert.json"}
    files = {"cert": {"b64": __import__("base64").b64encode(json.dumps(p.cert()).encode()).decode()}}
    res = human.prepare_res("member.add", args, files=files)
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
    res = human.prepare_res("member.add", args, files=files)
    assert err(res) == "sign.convenience_readers"


def test_member_add_works_after_switching_off_when_there_are_no_convenience_events(click, served, human):
    from tests.identity.helpers import Person

    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    q = Person()
    args = {"name": "q", "pk": crypto.b64u(q.pk_pub), "cert": "cert.json"}
    files = {"cert": {"b64": __import__("base64").b64encode(json.dumps(q.cert()).encode()).decode()}}
    ok(human.prepare_res("member.add", args, files=files))


# --- the click signer -------------------------------------------------------------------------------------------------


def test_a_click_is_signed_end_to_end_with_auth_convenience(click, served, human, monkeypatch):
    from orch.custody import passphrase
    from orch.ops import human as terminal

    def no_prompt(*a, **kw):
        raise AssertionError("click signing must never prompt on the terminal")

    monkeypatch.setattr(passphrase, "tty_passphrase_provider", no_prompt)
    monkeypatch.setattr(terminal, "review_prompt", no_prompt)
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "via a click"})
    assert p["confirm"] == "click"
    assert "code" not in pending(served, p["request_id"])  # bound at prepare: no hand-off code
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
    h = human.prepare_unbound("log", {"ref": key, "text": "x"})  # not the click signer's: another client's request
    rid = h["request"]["id"]
    refused(lambda: click.view(rid), "human.click_refused", "not_prepared_here")
    refused(lambda: click.sign(rid, "sha256:" + "0" * 64), "human.click_refused", "not_prepared_here")
    assert pending(served, rid)["state"] == "prepared"  # untouched: still the terminal's


def test_terminal_only_ops_are_refused_before_the_host_is_asked(click, served, human):
    for op in ("frobnicate", "init", "member.rename"):
        refused(lambda op=op: click.prepare(op, {}), "human.click_refused", "terminal_only")
    assert click.conns == []


def test_the_publish_server_is_a_click_and_the_view_shows_where_files_go(click, served, human):
    """#1198 C2: Publish's settings are a plain click; the View lists the server files are sent to and the address
    links open at (both signed), so the person reads the destination before the click."""
    value = {"server": "http://127.0.0.1:8103", "share": "http://127.0.0.1:8104"}
    p = click.prepare("settings.set", {"set": {"publish": value}})
    shown = click.view(p["request_id"])
    v = shown["view"]
    assert v["confirm"] == {"kind": "click"}
    text = "\n".join(v["lines"])
    assert value["server"] in text and value["share"] in text
    rows = json.dumps(v["content"])
    assert "Publish: files are sent to" in rows and value["server"] in rows
    assert "Publish: links open at" in rows and value["share"] in rows
    click.mono.t += 2
    assert click.sign(p["request_id"], shown["view_digest"])["duplicate"] is False
    assert click.signatures == 1
    removed = click.prepare("settings.set", {"set": {"publish": None}})
    assert "removed" in json.dumps(click.view(removed["request_id"])["view"]["content"])


def test_publish_share_and_the_publish_grant_pass_the_early_class_check(click, served, human):
    """#1198 C2: neither is refused as ``terminal_only`` any more; an unknown staged copy is the host's own refusal."""
    with pytest.raises(ClickError) as info:
        click.prepare("publish.share", {"staged_id": "ps_" + "0" * 32})
    assert info.value.reason != "terminal_only"
    with pytest.raises(ClickError) as info:
        click.prepare("addon.grant", {"name": "publish"})
    assert info.value.reason != "terminal_only"


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


def test_a_stale_reused_connection_is_retried_once_for_reads_only(click, served, human):
    _, key = human.ticket()
    click.signer._call(click.entry, "ticket.get", {"ref": key})  # connects and caches
    first = click.signer.conns[served.wid]

    def dead(op, args, **kw):
        raise BrokenPipeError("idle socket closed")

    first.call = dead
    assert click.signer._call(click.entry, "ticket.get", {"ref": key})["data"]  # fresh connection, no error
    assert click.signer.conns[served.wid] is not first
    click.signer.conns[served.wid].call = dead
    refused(lambda: click.signer._call(click.entry, "human.cancel", {"request_id": "x"}), "host.unavailable")


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


def test_op_classes_fail_closed_and_every_former_word_op_is_a_click():
    assert op_class("approve") == "click" and op_class("member.remove") == "click" and op_class("policy.set") == "click"
    assert op_class("grant") == "click" and op_class("some.future.op") == "terminal"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"grant_hours": 4}}]) == "click"  # B8
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"claim_ttl_min": 30}}]) == "click"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"repos": {}}}]) == "click"
    assert op_class("settings.set", [{"type": "settings.changed", "set": {"publish": {}}}]) == "click"
    assert op_class("settings.set", [{"type": "settings.changed", "set": "odd"}]) == "terminal"  # fails closed


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
    assert out["contract"] == 3 and out["core"]
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


def test_the_dashboard_side_never_imports_custody_code(tmp_path):
    code = textwrap.dedent(
        """
        import sys
        import orch.click.spawn, orch.click.client
        bad = [m for m in sys.modules if m.startswith(("orch.custody", "orch.click.signer", "orch.click.view"))]
        print(bad)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=child_env(tmp_path)
    )
    assert out.stdout.strip() == "[]", out.stdout


def test_the_real_child_answers_status_and_exits_when_the_channel_closes(tmp_path):
    from orch.click import spawn

    env = child_env(tmp_path, ORCH_STATE_DIR=str(tmp_path / "state"))
    client = spawn.start(env)
    try:
        out = client.call("status", {}, timeout=60)
        assert out["contract"] == 3 and out["workspaces"] == []
        with pytest.raises(ClickError) as info:
            client.call("view", {"workspace_id": "0" * 32, "request_id": "hr_" + "0" * 26}, timeout=60)
        assert info.value.code == "not_found"
    finally:
        client.close()
    assert client.proc.returncode is not None


def test_an_agent_cannot_start_the_signer(tmp_path):
    import socket

    a, b = socket.socketpair()
    try:
        env = child_env(tmp_path, ORCH_GRANT="x", ORCH_STATE_DIR=str(tmp_path))
        n = b.fileno()
        p = subprocess.run(
            [sys.executable, "-I", "-m", "orch.click", "--fd", str(n)], pass_fds=(n,), env=env, capture_output=True
        )
        assert p.returncode == 3
    finally:
        a.close()
        b.close()


# --- review round (#407 implementation reviews) ---------------------------------------------------------------


def _member_args():
    import base64

    from tests.identity.helpers import Person

    q = Person()
    args = {"name": "q", "pk": crypto.b64u(q.pk_pub), "cert": "cert.json"}
    return args, {"cert": {"b64": base64.b64encode(json.dumps(q.cert()).encode()).decode()}}


def _click_sign(click, key, text="via a click"):
    p = click.prepare("log", {"ref": key, "text": text})
    v = click.view(p["request_id"])
    click.mono.t += 2
    return click.sign(p["request_id"], v["view_digest"])


def test_a_member_add_prepared_before_convenience_events_is_refused_at_binding(served, human):
    c = Click(served, enable=False)
    try:
        args, files = _member_args()
        h = human.prepare_unbound("member.add", args, files=files)  # flag off, no history: allowed
        convenience.set_enabled(served.lab.state, served.wid, True, UID)
        _, key = human.ticket()
        _click_sign(c, key)  # a convenience event lands
        res = human.get(h["request"]["id"], signer=human.signer)  # orch sign CODE binds with the passphrase key
        assert err(res) == "sign.convenience_readers"
    finally:
        c.close()


def test_a_member_add_signed_before_convenience_events_is_refused_at_submit(served, human):
    c = Click(served, enable=False)
    try:
        args, files = _member_args()
        prepared = human.prepare("member.add", args, files=files)  # bound at prepare, flag off
        signed = human.sign(prepared)
        convenience.set_enabled(served.lab.state, served.wid, True, UID)
        _, key = human.ticket()
        _click_sign(c, key)
        assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "sign.convenience_readers"
        # switching off does not help: the history holds convenience events
        convenience.set_enabled(served.lab.state, served.wid, False, UID)
        assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "sign.convenience_readers"
    finally:
        c.close()


def test_every_view_confirms_by_a_click_and_a_stray_word_is_ignored(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    assert p["confirm"] == "click"
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    click.mono.t += 2
    assert click.sign(p["request_id"], v["view_digest"], word="whatever")["result"]["key"] == key  # not read
    assert click.signatures == 1


def _sign_with_tamper(click, human, change):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2

    def tamper(op, args, res):
        if op == "human.get":
            change(res["data"])
        return res

    click.tamper = tamper
    return p, v


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["events"][0].update(text="something else"),
        lambda d: d["material"].update(kind="payload", note="x"),
        lambda d: d["origin"].update(pid=d["origin"]["pid"] + 1),
        lambda d: d.update(
            checks=[{"repo": "other", "clean": True, "attested_by": "worker", "at": "2026-10-12T00:00:00Z"}]
        ),
    ],
    ids=["event", "material", "origin", "checks"],
)
def test_a_host_answer_that_changed_between_view_and_sign_is_refused(click, served, human, change):
    p, v = _sign_with_tamper(click, human, change)
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "view_changed")
    assert click.signatures == 0


def test_a_lying_host_at_prepare_is_refused_as_check_failed(click, served, human):
    _, key = human.ticket()

    def lie(op, args, res):
        if op == "human.prepare":
            res["data"]["review_digest"] = "sha256:" + "0" * 64
        return res

    click.tamper = lie
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "human.click_refused", "check_failed")

    def other_op(op, args, res):
        if op == "human.prepare":
            res["data"]["op"] = "set"
        return res

    click.tamper = other_op
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "human.click_refused", "check_failed")


def test_an_unclean_worker_check_is_refused(click, served, human):
    def dirty(d):
        d["checks"] = [{"repo": "r", "clean": False, "attested_by": "worker", "at": "2026-10-12T00:00:00Z"}]

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    click.tamper = lambda op, args, res: (dirty(res["data"]) or res) if op == "human.get" else res
    refused(lambda: click.view(p["request_id"]), "human.click_refused", "check_failed")


def test_busy_budget_and_caps(click, served, human, monkeypatch):
    from orch.click import signer as click_signer

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    click.signer.busy = True
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "busy")
    click.signer.busy = False
    click.signer.budget = Budget(click.mono, per_minute=0)
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "rate.limited")
    assert click.signatures == 0 and pending(served, p["request_id"])["state"] == "bound"
    monkeypatch.setattr(click_signer, "MAX_APPROVALS", 1)
    refused(lambda: click.prepare("log", {"ref": key, "text": "y"}), "rate.limited")


def test_too_many_events_or_too_large_a_view_go_to_the_terminal(click, served, human, monkeypatch):
    _, key = human.ticket()

    def many(op, args, res):
        if op == "human.prepare":
            res["data"]["events"] = res["data"]["events"] * 17
        return res

    click.tamper = many
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "human.click_refused", "terminal_only")
    click.tamper = None
    monkeypatch.setattr(views, "MAX_VIEW_BYTES", 100)
    p = click.prepare("log", {"ref": key, "text": "x"})
    refused(lambda: click.view(p["request_id"]), "human.click_refused", "terminal_only")


def test_an_idem_conflict_at_submit_reads_the_request_state_and_signs_nothing_again(click, served, human):
    from orch.host.errors import HostError

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2

    def conflict(op, args, res):
        if op == "human.submit":
            raise HostError("idem.conflict", "taken")
        return res

    click.tamper = conflict
    e = refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "uncertain")
    assert "idem.conflict" in e.message and "the request is" in e.message
    assert click.signatures == 1  # signed once; a retry resends, never signs again
    click.tamper = None
    assert click.sign(p["request_id"], v["view_digest"])["result"]["key"] == key
    assert click.signatures == 1


def test_a_custody_change_after_prepare_is_refused_at_view(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    click.gen += 1
    refused(lambda: click.view(p["request_id"]), "human.click_refused", "custody_changed")


def test_artifact_bytes_the_dashboard_uploaded_are_labelled_checked(click, served, human):
    import base64

    _, key = human.ticket()
    files = {"path": {"b64": base64.b64encode(b"the proof\n").decode()}}
    p = click.prepare("artifact.add", {"ref": key, "path": "proof.txt"}, files=files)
    v = click.view(p["request_id"])
    assert v["view"]["artifacts"] == "checked"


def test_a_request_cancelled_at_the_host_is_reported_not_signed(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    ok(served.client().req("human.cancel", {"request_id": p["request_id"]}))
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.request_state")
    assert click.signatures == 0


def test_status_reports_version_mismatch_and_locked_custody(click, served, human):
    from orch.custody.base import BackendUnavailable

    _, key = human.ticket()
    welcome_check = ClickSigner.__dict__["_check_welcome"]  # the staticmethod object itself
    try:
        ClickSigner._check_welcome = staticmethod(lambda w: (_ for _ in ()).throw(ClickError("host.version_mismatch")))
        click.signer.close()
        refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "host.version_mismatch")
    finally:
        ClickSigner._check_welcome = welcome_check
    (row,) = click.signer.status({})["workspaces"]
    assert row["available"] is False and row["problem"] == "version_mismatch"
    click.signer._custody = lambda s, w: (_ for _ in ()).throw(BackendUnavailable("locked"))
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "custody.unavailable")
    (row,) = click.signer.status({})["workspaces"]
    assert row["problem"] in ("custody_unavailable", "version_mismatch")


def test_the_unavailable_reason_is_one_of_the_known_five(click, served, human):
    from orch.host.errors import HostError

    _, key = human.ticket()

    def odd(op, args, res):
        raise HostError("sign.convenience_unavailable", "something new: text")

    click.tamper = odd
    e = refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "sign.convenience_unavailable")
    assert e.reason is None


# --- #517: Error.detail for the two convenience codes, re-validated whole -----------------------------------------

DEV = "d_" + "0123456789abcdef" * 2


def _detail_from(click, human, code, detail):
    from orch.host.errors import HostError

    _, key = human.ticket()

    def refuse(op, args, res):
        exc = HostError(code, "x: y")
        exc.detail = detail  # in-process: what the client would hold, unfiltered
        raise exc

    click.tamper = refuse
    e = refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), code)
    click.tamper = None
    return e


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("sign.convenience_unavailable", {"reason": "phone_device", "devices": [DEV]}),
        ("sign.convenience_unavailable", {"reason": "disabled", "devices": []}),
        ("sign.convenience_readers", {"reason": "reader_too_old", "devices": [DEV, "d_" + "f" * 32]}),
        ("sign.convenience_readers", {"reason": "unknown_readers", "devices": [DEV] * 64}),
    ],
)
def test_a_valid_convenience_detail_is_forwarded_unchanged(click, served, human, code, detail):
    e = _detail_from(click, human, code, copy.deepcopy(detail))
    assert e.detail == detail and e.error()["detail"] == detail
    assert ClickError.of(e.error()).detail == detail  # the dashboard's client reads it back


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        ("sign.convenience_unavailable", {"reason": "reader_too_old", "devices": [DEV]}),  # the other code's reason
        ("sign.convenience_readers", {"reason": "disabled", "devices": [DEV]}),
        ("sign.convenience_unavailable", {"reason": "nope", "devices": [DEV]}),
        ("sign.convenience_readers", {"reason": "no_declaration", "devices": ["d_" + "A" * 32]}),
        ("sign.convenience_readers", {"reason": "no_declaration", "devices": [DEV + "0"]}),
        ("sign.convenience_readers", {"reason": "no_declaration", "devices": [DEV, 7]}),
        ("sign.convenience_unavailable", {"reason": "phone_device", "devices": [DEV] * 65}),
        ("sign.convenience_unavailable", {"reason": "phone_device", "devices": [DEV], "path": "/x"}),
        ("sign.convenience_unavailable", {"reason": "phone_device", "devices": DEV}),
        ("sign.convenience_readers", {"reason": "no_declaration", "devices": {DEV: 1}}),
        ("sign.convenience_readers", {"reason": ["no_declaration"], "devices": [DEV]}),
        ("sign.convenience_readers", {}),
        ("sign.convenience_readers", {"reason": "no_declaration"}),  # the host always sends both keys
        ("sign.convenience_unavailable", {"devices": [DEV]}),
        ("sign.convenience_unavailable", {"reason": True, "devices": [DEV]}),  # bools are not strings
        ("sign.convenience_unavailable", {"reason": "disabled", "devices": [True]}),
        ("sign.convenience_unavailable", {"reason": "disabled", "devices": True}),
        ("sign.convenience_readers", {"reason": None, "devices": []}),
        ("sign.convenience_readers", ["no_declaration"]),
    ],
)
def test_an_invalid_convenience_detail_gives_no_detail_at_all(click, served, human, code, detail):
    e = _detail_from(click, human, code, detail)
    assert e.detail is None and "detail" not in e.error()
    assert ClickError.of({"code": code, "detail": detail}).detail is None


def test_the_forwarded_detail_follows_the_hosts_closed_schema():
    """One source of truth (#517): the signer's sets are the host's, and the host sends exactly ``{reason, devices}``
    for both codes; a host change that adds a reason or a key shows up here, not as silently dropped detail."""
    from orch.click import protocol
    from orch.host import convenience as hconv
    from orch.host.errors import DETAIL_KEYS, DEVICE_ID, DEVICES, DEVICES_MAX, closed_detail
    from orch.ops.readers import ReadersRefused

    codes = ("sign.convenience_unavailable", "sign.convenience_readers")
    assert set(protocol.DETAIL_REASONS) == set(codes)
    for code in codes:
        assert set(DETAIL_KEYS[code]) == protocol.DETAIL_FIELDS and DETAIL_KEYS[code]["devices"] == DEVICES
        assert protocol.DETAIL_REASONS[code] == frozenset(DETAIL_KEYS[code]["reason"])
    assert set(protocol.REASONS) == protocol.DETAIL_REASONS["sign.convenience_unavailable"]
    assert set(hconv._TEXT) <= protocol.DETAIL_REASONS["sign.convenience_unavailable"]
    assert DEVICES_MAX == 64 and DEVICE_ID.pattern == r"d_[0-9a-f]{32}"
    assert DEVICE_ID.fullmatch(DEV)
    # what the host raises, through its own socket filter, is forwarded whole
    for exc in (
        hconv.unavailable("phone_device", [DEV]),
        hconv.unavailable("disabled", []),
        ReadersRefused("sign.convenience_readers", "reader_too_old", "x", devices=[DEV]),
        ReadersRefused("sign.convenience_unavailable", "other_member", "x", devices=[DEV]),
    ):
        wire = closed_detail(exc.code, exc.detail)
        assert wire == exc.detail and ClickError.of({"code": exc.code, "detail": wire}).detail == wire


def test_any_other_code_drops_its_detail(click, served, human):
    e = _detail_from(click, human, "launch.no_grant", {"reason": "none"})
    assert e.detail is None and "detail" not in e.error()
    e = _detail_from(click, human, "lock.busy", {"reason": "disabled", "devices": [DEV]})
    assert e.detail is None and "detail" not in e.error()
    assert (
        ClickError("human.click_refused", "x", reason="busy", detail={"reason": "disabled"}).error().get("detail")
        is None
    )


def _spawn_raw(tmp_path):
    import socket

    a, b = socket.socketpair()
    env = child_env(tmp_path, ORCH_STATE_DIR=str(tmp_path / "state"))
    p = subprocess.Popen(
        [sys.executable, "-I", "-m", "orch.click", "--fd", str(b.fileno())], pass_fds=(b.fileno(),), env=env
    )
    b.close()
    return a, p


def test_a_framing_error_or_a_bad_request_id_closes_the_channel(tmp_path):
    for junk in (b"\x00\x00\x00\x05nope!", b'\x00\x00\x00\x32{"t":"req","id":"bad id!","op":"status","args":{}}'):
        a, p = _spawn_raw(tmp_path)
        try:
            a.settimeout(30)
            a.sendall(junk)
            assert a.recv(100) == b""
            assert p.wait(30) == 0
        finally:
            a.close()
            if p.poll() is None:
                p.kill()


def test_the_signer_exits_when_its_parent_goes(tmp_path):
    import os
    import socket
    import time

    from orch.click.client import ClickClient

    a, b = socket.socketpair()
    n = b.fileno()
    middle = textwrap.dedent(
        f"""
        import subprocess, sys
        p = subprocess.Popen(
            [sys.executable, "-I", "-m", "orch.click", "--fd", "{n}"],
            pass_fds=({n},), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(p.pid, flush=True)
        sys.stdin.readline()   # stay the parent until the test says so
        """
    )
    env = child_env(tmp_path, ORCH_STATE_DIR=str(tmp_path / "state"))
    mid = subprocess.Popen(
        [sys.executable, "-c", middle], pass_fds=(n,), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    b.close()
    pid = int(mid.stdout.readline().strip())
    try:
        assert ClickClient(a).call("status", {}, timeout=60)["contract"] == 3  # running, its parent watch started
        mid.stdin.write("go\n")
        mid.stdin.flush()
        mid.wait(30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # the channel stays open (we hold `a`): only the parent watch ends it
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the signer outlived its parent")
    finally:
        a.close()
        if mid.poll() is None:
            mid.kill()
        try:
            os.kill(pid, 9)  # our own grandchild's PID only, if it is still there
        except ProcessLookupError:
            pass


# --- the durable convenience_seen mark (#441) -------------------------------------------------------------------------


def test_the_first_convenience_event_sets_the_mark_and_a_restore_does_not_clear_it(click, served, human, monkeypatch):
    from orch.ops import readers

    path = readers.seen_path(served.lab.state, served.wid)
    _, key = human.ticket()
    assert not path.exists()
    _click_sign(click, key)
    assert path.exists() and any(e.get("auth") == "convenience" for e in logged(served, key))
    # a restore drops the convenience events from the merged history: the mark still refuses a new reader
    monkeypatch.setattr(readers, "has_convenience_events", lambda store: False)
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    args, files = _member_args()
    assert err(human.prepare_res("member.add", args, files=files)) == "sign.convenience_readers"


def test_a_mark_that_cannot_be_written_refuses_the_convenience_append(click, served, human, monkeypatch):
    from orch.ops import readers

    def boom(*_a, **_k):
        raise OSError("no space left")

    monkeypatch.setattr(readers, "ensure_convenience_mark", boom)
    _, key = human.ticket()
    before = len(logged(served, key))
    with pytest.raises(ClickError) as info:
        _click_sign(click, key)
    assert info.value.code == "custody.unsafe_mode", info.value
    assert len(logged(served, key)) == before
    assert not readers.seen_path(served.lab.state, served.wid).exists()


def test_an_existing_workspace_with_convenience_history_counts_as_seen(click, served, human, monkeypatch):
    from orch.ops import readers

    _, key = human.ticket()
    _click_sign(click, key)
    path = readers.seen_path(served.lab.state, served.wid)
    path.unlink()  # a workspace from before the mark: only the merged history knows
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    args, files = _member_args()
    assert err(human.prepare_res("member.add", args, files=files)) == "sign.convenience_readers"
    from orch.host.errors import HostError

    service = served.service
    with service.store.locked(), pytest.raises(HostError) as info:  # the host's own check (binding and submit)
        convenience.refuse_reader_op(service, "member.add", history=True)
    assert info.value.code == "sign.convenience_readers"
    assert path.exists()  # the scan that found them set the mark


def test_c21_a_missing_key_refuses_before_the_claim_and_the_request_stays_signable(click, served, human):
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "key back"})
    rid = p["request_id"]
    v = click.view(rid)
    click.mono.t += 2
    gone = click.store.items.pop((served.wid, click.owner.device))
    e = refused(lambda: click.sign(rid, v["view_digest"]), "custody.no_key")
    assert "no signing key" in e.message
    assert click.signatures == 0 and pending(served, rid)["state"] == "bound"
    assert click.signer.records.get(served.wid, rid) is None  # nothing was claimed
    assert (served.wid, rid) not in click.signer._attempted
    click.store.items[(served.wid, click.owner.device)] = gone  # the key is back: the same request signs
    res = click.sign(rid, v["view_digest"])
    assert res["duplicate"] is False and click.signatures == 1


def test_c21_an_unreadable_key_store_is_unavailable_not_uncertain(click, served, human, monkeypatch):
    from orch.custody.base import CustodyError

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "locked"})
    v = click.view(p["request_id"])
    click.mono.t += 2

    def locked(*a):
        raise CustodyError("locked")

    monkeypatch.setattr(click.store, "get", locked)
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "custody.unavailable")
    assert click.signatures == 0 and click.signer.records.get(served.wid, p["request_id"]) is None
    monkeypatch.undo()
    assert click.sign(p["request_id"], v["view_digest"])["duplicate"] is False


def test_c21_a_brief_store_lock_does_not_leave_one_click_unavailable(click, served, human, monkeypatch):
    from orch.custody.base import CustodyError

    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "lock"})
    v = click.view(p["request_id"])
    click.mono.t += 2
    ok_status = lambda st, w: {"custody": "convenience", "device": click.owner.device}  # noqa: E731
    row = lambda: click.signer._row(click.entry, ok_status)  # noqa: E731

    def locked(*a):
        raise CustodyError("locked")

    monkeypatch.setattr(click.store, "get", locked)
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "custody.unavailable")
    assert row()["available"] is False
    monkeypatch.undo()
    assert click.sign(p["request_id"], v["view_digest"])["duplicate"] is False
    assert row()["available"] is True


def test_device_remove_is_a_click_and_the_view_names_the_device(click, served, human, lab):
    """#1198 C15: removing a device is a plain click. The View names the device, whose it is and what follows.
    The device that signs the click is refused with its own plain reason (the lockout guard). The reader that fails
    the readers condition may be removed by click: the rule runs on the state after the removal."""
    member = lab.add_member()  # has not declared that it knows convenience signatures: it fails the readers rule
    other = member.device
    p = click.prepare("device.remove", {"device": other})
    shown = click.view(p["request_id"])
    v = shown["view"]
    assert v["confirm"] == {"kind": "click"}
    text = "\n".join(v["lines"]) + json.dumps(v["content"])
    assert other in text and "can no longer sign" in text and "belongs to" in text and "quinn" in text
    click.mono.t += 2
    assert click.sign(p["request_id"], shown["view_digest"])["duplicate"] is False
    with pytest.raises(ClickError) as info:
        click.prepare("device.remove", {"device": click.owner.device})
    assert info.value.code == "device.lockout" and "device you are signing with" in info.value.message


def test_a_removal_cannot_clear_the_readers_rule_while_another_reader_still_fails_it(click, served, human, lab):
    """Q1 (Opus): only the device being removed is judged as gone. Another undeclared reader still blocks the click,
    and so does any other op."""
    first, second = lab.add_member(), lab.add_member()
    with pytest.raises(ClickError) as info:
        click.prepare("device.remove", {"device": first.device})
    assert info.value.code == "sign.convenience_unavailable" and second.device in (info.value.detail or {}).get(
        "devices", []
    )
    _, key = human.ticket()
    with pytest.raises(ClickError):  # no blanket exemption: other ops stay refused
        click.prepare("log", {"ref": key, "text": "x"})


def test_leaving_the_hub_by_click_ignores_the_readers_reasons(click, served, human):
    """e2e-4: a full-mode workspace is relay paired, so every click was refused; hub.leave is the way out."""
    (served.lab.state / "hosts" / served.wid / "membership").mkdir(parents=True)
    svc, dev = served.service, human.person.device
    with pytest.raises(Exception, match="relay_paired"):
        convenience.check(svc, human.person.ref, dev)
    convenience.check(svc, human.person.ref, dev, leaving=True)
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    with pytest.raises(Exception, match="disabled"):
        convenience.check(svc, human.person.ref, dev, leaving=True)  # the flag still decides


@pytest.mark.parametrize("reason", ["other_member", "phone_device", "reader_version"])
def test_leaving_the_hub_still_refuses_other_readers(click, served, human, monkeypatch, reason):
    """Only relay_paired is waived for hub.leave; another reader below level 1 or an old model still refuses it.
    (`why` imports levels/below_level from orch.ops.readers at call time, so patching that module takes effect.)"""
    from orch.ops import readers

    (served.lab.state / "hosts" / served.wid / "membership").mkdir(parents=True)
    if reason == "reader_version":
        monkeypatch.setattr(convenience, "_model_knows", lambda: False)
    else:
        monkeypatch.setattr(readers, "levels", lambda *a, **k: {})
        monkeypatch.setattr(readers, "below_level", lambda *a, **k: (reason, ["d_x"]))
    svc, dev = served.service, human.person.device
    with pytest.raises(Exception, match=reason):
        convenience.check(svc, human.person.ref, dev, leaving=True)
