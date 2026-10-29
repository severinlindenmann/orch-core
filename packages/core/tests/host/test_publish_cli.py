"""P5: ``orch publish`` (the CLI of the Publish add-on): who may run what, the order of signing and sending, and that
the link is written to the person's terminal and nowhere else. The host half is ``tests/host/test_publishing.py``; here
the host is real for the plain commands and replaced where a terminal and a key would be needed."""

from __future__ import annotations

import io
import json
import re
from types import SimpleNamespace

import pytest

from orch.cli import publish
from orch.cli.main import main as orch_main
from orch.ops import human
from tests.host import human_helpers
from tests.host.test_publishing import ADDON, REPORT, SERVER, SHARE, World

lab, served, human_fx = human_helpers.lab, human_helpers.served, human_helpers.human


def run(argv, env, *, as_json=False):
    out, err = io.StringIO(), io.StringIO()
    code = publish.main([*argv, *(["--json"] if as_json else [])], env=env, out=out, err=err)
    return code, out.getvalue()


NOT_CALLED = SimpleNamespace()


def never(*a, **k):
    raise AssertionError("the host must not be reached")


@pytest.fixture
def offline(monkeypatch):
    """No host at all: any command that gets as far as the host fails the test."""
    monkeypatch.setattr(publish, "_target", never)
    monkeypatch.setattr(publish, "_call", never)
    monkeypatch.setattr(publish, "_sign", never)


@pytest.mark.parametrize(
    "argv",
    [["share", "ps_" + "0" * 32, "--yes"], ["send", "ps_" + "0" * 32], ["revoke", "0" * 32], ["register"],
     ["extend", "0" * 32, "--days", "3"], ["password", "0" * 32],
     ["setup", "--server", SERVER, "--share", SHARE]],
)  # fmt: skip
def test_an_agent_can_not_share_send_revoke_register_or_set_up(offline, argv):
    code, out = run(argv, {"ORCH_GRANT": "gr_x", "ORCH_SESSION": "s.1"})
    assert code == 3 and out.startswith("error op.not_allowed: a person does this; an agent only prepares")


def test_an_agent_refusal_in_json(offline):
    code, out = run(["share", "ps_" + "0" * 32, "--yes"], {"ORCH_GRANT": "gr_x"}, as_json=True)
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed"


@pytest.mark.parametrize(
    "argv",
    [[], ["bogus"], ["prepare"], ["prepare", "TST-0001"], ["share"], ["share", "a", "b"], ["revoke"], ["discard"],
     ["setup"], ["setup", "--server", SERVER], ["list", "a", "b"], ["extend"], ["extend", "a" * 32],
     ["extend", "a" * 32, "--days", "0"], ["extend", "a" * 32, "--days", "31"], ["password"], ["password", "a", "b"],
     ["prepare", "TST-0001", "a.md", "--days", "x"]],
)  # fmt: skip
def test_a_malformed_command_prints_the_usage_and_touches_nothing(offline, argv):
    code, out = run(argv, {})
    assert code == 2 and "usage: orch publish" in out


@pytest.mark.parametrize(
    "server",
    ["http://example.com", "https://Pub.example.com", "https://pub.example.com/v1", "ftp://x", "pub.example.com"],
)
def test_setup_refuses_an_address_that_is_not_allowed_before_any_signature(offline, server):
    code, out = run(["setup", "--server", server, "--share", "https://apps.example.com"], {})
    assert code == 5 and "error invalid.input" in out and "no path" in out


def test_setup_accepts_one_address_for_both_no_domain_single_origin(offline):
    from types import SimpleNamespace as NS

    from orch.host import seqaddon

    publish._check_setup(NS(server="http://127.0.0.1:8102", share_addr="http://127.0.0.1:8102"))
    one = {"server": "http://127.0.0.1:8102", "share": "http://127.0.0.1:8102"}
    assert seqaddon._setting("publish", "publish", one, ValueError) == one


def test_setup_and_share_stop_before_anything_when_there_is_no_terminal(monkeypatch):
    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_tty_ok", lambda: False)
    monkeypatch.setattr(publish, "_sign", never)
    monkeypatch.setattr(publish, "_call", never)
    for argv in (
        ["setup", "--server", SERVER, "--share", SHARE],
        ["share", "ps_" + "0" * 32, "--yes"],
        ["send", "ps_" + "0" * 32],
    ):
        code, out = run(argv, {})
        assert code in (3, 5) and "no terminal" in out, argv
    assert "nothing was signed" in run(["share", "ps_" + "0" * 32, "--yes"], {})[1]


class Rec:
    """A stand-in for the host: records the order of calls; ``send`` returns a link."""

    def __init__(self):
        self.calls, self.shown = [], []
        self.token = "T" * 43

    def target(self, env):
        return SimpleNamespace()

    def call(self, target, op, args, env):
        self.calls.append((op, args))
        if op == "publish.send":
            return {"share_id": "a" * 32, "ticket": "TST-0001", "kind": "secret", "expires_at": "2026-10-25T10:00:00Z",
                    "link": f"{SHARE}/s/{'a' * 32}#t={self.token}", "view_limit": 0, "shown_once": True}  # fmt: skip
        if op == "publish.password":
            return {"share_id": args["share_id"], "password": "bada-fobu-gele-kimo-ruzi", "shown_once": True}
        if op == "publish.extend":
            return {"share_id": args["share_id"], "expires_at": "2026-10-25T10:00:00Z"}
        return {}

    def sign(self, target, op, args):
        self.calls.append(("sign:" + op, args))
        return {}


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(publish, "_target", r.target)
    monkeypatch.setattr(publish, "_call", r.call)
    monkeypatch.setattr(publish, "_sign", r.sign)
    monkeypatch.setattr(publish, "_tty_ok", lambda: True)
    monkeypatch.setattr(human, "show_secret", r.shown.append)
    return r


def test_share_signs_first_then_sends_and_the_link_goes_to_the_terminal_only(rec):
    staged = "ps_" + "1" * 32
    code, out = run(["share", staged], {})
    assert code == 0
    assert [c[0] for c in rec.calls] == ["sign:publish.share", "publish.send"]
    assert rec.calls[0][1] == {"staged_id": staged}
    (shown,) = rec.shown
    assert f"#t={rec.token}" in shown and "You will not see this link again" in shown and "2026-10-25" in shown
    assert rec.token not in out and "#t=" not in out and "/s/" not in out  # stdout never carries it
    assert "the link is on your terminal only" in out


def test_send_alone_does_not_sign_and_json_still_hides_the_link(rec):
    code, out = run(["send", "ps_" + "1" * 32], {}, as_json=True)
    assert code == 0 and [c[0] for c in rec.calls] == ["publish.send"]
    doc = json.loads(out)
    assert doc["data"]["link_shown"] is True and "link" not in doc["data"] and rec.token not in out


def test_a_link_that_cannot_be_shown_is_not_printed_anywhere_else(rec, monkeypatch):
    def broken(text):
        raise human.NoPrompt("no terminal")

    monkeypatch.setattr(human, "show_secret", broken)
    code, out = run(["send", "ps_" + "1" * 32], {})
    assert code == 1 and rec.token not in out and "orch publish revoke " + "a" * 32 in out


def test_a_refused_signature_sends_nothing(rec, monkeypatch):
    def refused(target, op, args):
        raise publish.PubError("invalid.input", "not confirmed: nothing was signed", 5)

    monkeypatch.setattr(publish, "_sign", refused)
    code, out = run(["share", "ps_" + "1" * 32], {})
    assert code == 5 and rec.calls == [] and "nothing was signed" in out


def test_prepare_passes_the_choices_and_says_nothing_left_the_computer(rec, monkeypatch):
    monkeypatch.setattr(
        publish, "_call", lambda t, op, args, env: rec.calls.append((op, args)) or {
            "staged_id": "ps_" + "2" * 32, "files": [{}, {}], "kind": args.get("kind", "secret"),
            "views": args.get("views", 0), "days": args["days"], "ticket": "TST-0001"},
    )  # fmt: skip
    code, out = run(["prepare", "TST-0001", "a.md", "b.md", "--once", "3", "--days", "30"], {})
    assert code == 0 and rec.calls == [
        ("publish.prepare", {"ref": "TST-0001", "artifacts": ["a.md", "b.md"], "days": 30, "kind": "once", "views": 3})
    ]
    assert "2 file(s), show-once link (3 view(s)), 30 days" in out and "nothing has left this computer" in out
    code, out = run(["prepare", "TST-0001", "a.md", "--once"], {})
    assert rec.calls[-1][1]["views"] == 1 and rec.calls[-1][1]["kind"] == "once"


# -- the real thing: this CLI against a served workspace -------------------------------------------------------------


def person_env(w):
    return {**w.served.lab.ws.env(session=None, grant=False), "ORCH_HOST_RUN_DIR": str(w.served.lab.run)}


def test_prepare_list_and_discard_through_the_real_host(served, human_fx, monkeypatch):
    w = World(served, human_fx, monkeypatch)
    w.append_workspace("addon.granted", **ADDON)
    w.configure()
    uid, key = w.ticket_with(**{"report.md": REPORT})
    env = person_env(w)
    code, out = run(["prepare", key, "report.md"], env)
    assert code == 0, out
    staged = re.search(r"ps_[0-9a-f]{32}", out).group(0)
    assert f"orch publish share {staged}" in out
    code, out = run(["list"], env)
    assert code == 0 and f"staged {staged} {key} secret 1 file(s) by you" in out
    code, out = run(["list", "--json"], env)
    data = json.loads(out)["data"]
    assert data["configured"] and data["staged"][0]["staged_id"] == staged
    code, out = run(["discard", staged], env)
    assert code == 0 and out.strip() == f"ok publish discarded {staged}"
    assert run(["list"], env)[1].strip() == "nothing staged or shared"
    code, out = run(["discard", staged], env)
    assert code == 2 and out.startswith("error not_found")
    assert w.server.requests == []


def test_the_cli_reaches_the_host_only_through_orch_main(served, human_fx, capsys, monkeypatch):
    w = World(served, human_fx, monkeypatch)
    w.append_workspace("addon.granted", **ADDON)
    w.configure()
    out, err = io.StringIO(), io.StringIO()
    code = orch_main(["publish", "list"], env=person_env(w), stdout=out, stderr=err)
    assert code == 0 and out.getvalue().strip() == "nothing staged or shared"
    code = orch_main(["publish", "revoke", "0" * 32], env={**person_env(w), "ORCH_GRANT": "gr_x"}, stdout=out,
                     stderr=err)  # fmt: skip
    assert code == 3 and "op.not_allowed" in out.getvalue()


def test_without_a_host_the_command_says_so(tmp_path):
    code, out = run(["list"], {"ORCH_WORKSPACE": str(tmp_path), "HOME": str(tmp_path)})
    assert code == 3 and out.startswith("error ") and ("host" in out or "workspace" in out)


def test_an_unknown_workspace_prints_what_the_admin_must_run(monkeypatch):
    from orch.ops.errors import OrchError

    calls = []

    def call(target, op, args, env):
        calls.append(op)
        if op == "publish.register":
            raise OrchError("publish.server_refused", "the server no longer knows this workspace; ask its admin")
        return {"workspace": "1" * 32, "owner": "2" * 32}

    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_call", call)
    monkeypatch.setattr(
        publish, "_allow_on_local_server", lambda info, env: False
    )  # no publish server on this computer
    code, out = run(["register"], {})
    assert code == 3 and calls == ["publish.register", "publish.list"]
    assert f"orch-publish admin --data DATA_DIR owners add {'2' * 32}" in out
    assert f"orch-publish admin --data DATA_DIR ws allow {'1' * 32} {'2' * 32}" in out
    assert "Then run: orch publish register" in out
    assert f"--owner {'2' * 32} --workspace {'1' * 32}={'2' * 32}" in out


def test_register_allows_the_workspace_on_the_publish_server_installed_here(monkeypatch, tmp_path):
    import json
    import os

    from orch.install.state import Layout, save
    from orch.ops.errors import OrchError

    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    lay = Layout(env)
    log = tmp_path / "admin.log"
    tool = tmp_path / "orch-publish"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    os.chmod(tool, 0o755)
    save(lay, {"v": 1, "address": None, "domain": None, "workspaces": [], "parts": {"publish": {"binary": str(tool)}}})
    seen = []

    def call(target, op, args, env):
        seen.append(op)
        if op == "publish.register" and seen.count(op) == 1:
            raise OrchError("publish.server_refused", "the server no longer knows this workspace; ask its admin")
        return {"workspace": "1" * 32, "owner": "2" * 32, "result": "registered"}

    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_call", call)
    code, out = run(["register"], env)
    assert code == 0 and "ok publish register" in out, out
    lines = log.read_text().splitlines()
    data = str(lay.data("publish"))
    assert lines == [
        f"admin --data {data} owners add {'2' * 32}",
        f"admin --data {data} ws allow {'1' * 32} {'2' * 32}",
    ]
    assert json.loads(lay.file.read_text())  # state untouched


def test_register_without_a_hub_says_join_a_hub(monkeypatch):
    from orch.ops.errors import OrchError

    def call(target, op, args, env):
        raise OrchError("hub.not_joined", "publishing needs a hub: join a hub first (orch hub join)")

    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_call", call)
    code, out = run(["register"], {})
    assert code == 3 and "Join a hub (orch hub join)" in out


def test_list_shows_local_end_times_and_json_keeps_utc(monkeypatch):
    data = {
        "configured": True,
        "staged": [],
        "shares": [
            {
                "id": "s1",
                "ticket": "T-1",
                "kind": "secret",
                "state": "live",
                "expires_at": "2026-10-25T16:40:06Z",
                "views": 0,
            }
        ],
    }
    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_call", lambda *a: data)
    code, out = run(["list"], {})
    assert (
        code == 0
        and "2026-10-25T16:40:06Z" not in out
        and "until " in out
        and re.search(r"until \w{3} 2\d \w{3} \d\d:\d\d", out)
    )
    code, out = run(["list", "--json"], {})
    assert "2026-10-25T16:40:06Z" in out


def test_setup_with_the_addon_off_saves_and_says_what_is_next(monkeypatch):
    from orch.ops.errors import OrchError

    def call(target, op, args, env):
        raise OrchError("runner.disabled", "Publish is off")

    signed = []
    monkeypatch.setattr(publish, "_target", lambda env: SimpleNamespace())
    monkeypatch.setattr(publish, "_tty_ok", lambda: True)
    monkeypatch.setattr(publish, "_sign", lambda t, op, args: signed.append((op, args)) or {})
    monkeypatch.setattr(publish, "_call", call)
    code, out = run(["setup", "--server", SERVER, "--share", SHARE], {})
    assert signed == [("settings.set", {"set": {"publish": {"server": SERVER, "share": SHARE}}})]
    assert code == 3 and out.startswith("ok publish setup\n") and "Turn it on in Settings, Add-ons" in out


def test_revoke_asks_first_and_yes_skips_the_question(rec, monkeypatch):
    asked = []
    monkeypatch.setattr(publish, "review_prompt", lambda prompt, verb="sign": asked.append((prompt, verb)) or False)
    code, out = run(["revoke", "a" * 32], {})
    assert code == 3 and "not confirmed: nothing was revoked" in out and rec.calls == []
    assert asked and "stops working at once" in asked[0][0] and asked[0][1] == "continue"  # a revoke signs nothing
    monkeypatch.setattr(publish, "review_prompt", lambda prompt, verb="sign": asked.append("again") or True)
    code, out = run(["revoke", "a" * 32], {})
    assert code == 0 and rec.calls == [("publish.revoke", {"share_id": "a" * 32})]
    asked.clear()
    code, out = run(["revoke", "b" * 32, "--yes"], {})
    assert code == 0 and asked == [] and rec.calls[-1] == ("publish.revoke", {"share_id": "b" * 32})


def test_extend_sends_the_days_and_nothing_else(rec):
    code, out = run(["extend", "a" * 32, "--days", "30"], {})
    assert code == 0 and rec.calls == [("publish.extend", {"share_id": "a" * 32, "days": 30})]
    assert "ok publish extended" in out


def test_the_password_goes_to_the_terminal_only(rec):
    code, out = run(["password", "a" * 32], {})
    assert code == 0 and rec.calls == [("publish.password", {"share_id": "a" * 32})]
    (shown,) = rec.shown
    assert "bada-fobu-gele-kimo-ruzi" in shown and "shown once" in shown
    assert "bada" not in out and "the password is on your terminal only" in out
    code, out = run(["password", "a" * 32], {}, as_json=True)
    doc = json.loads(out)
    assert doc["data"]["password_shown"] is True and "password" not in doc["data"] and "bada" not in out


def test_a_password_that_cannot_be_shown_is_not_printed_anywhere_else(rec, monkeypatch):
    def broken(text):
        raise human.NoPrompt("no terminal")

    monkeypatch.setattr(human, "show_secret", broken)
    code, out = run(["password", "a" * 32], {})
    assert code == 1 and "bada" not in out and "orch publish password " + "a" * 32 in out
    monkeypatch.setattr(publish, "_tty_ok", lambda: False)
    code, out = run(["password", "a" * 32], {})
    assert code == 5 and "nothing was set" in out
