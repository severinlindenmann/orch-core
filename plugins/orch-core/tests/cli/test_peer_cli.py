"""V3 C12: ``orch peer``: the shapes S7 uses, and who may run what before any host is touched."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import peer
from orch.ops.errors import OrchError

WS = "bb" * 16


def run(argv, env, *, as_json=True):
    out = io.StringIO()
    code = peer.main([*argv, *(["--json"] if as_json else [])], env=env, out=out, err=io.StringIO())
    return code, out.getvalue()


@pytest.fixture
def host(monkeypatch):
    log = SimpleNamespace(called=[], reply={})
    monkeypatch.setattr(peer, "_target", lambda env: object())

    def call(t, op, args, env):
        log.called.append((op, args))
        r = log.reply.get(op, {})
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(peer, "_call", call)
    return log


@pytest.mark.parametrize(
    "argv",
    [["card"], ["add", "--card", "x"], ["send", WS, "--title", "t"], ["accept", f"{WS}/{WS}"],
     ["reply", f"{WS}/{WS}", "--summary", "s"]],
)  # fmt: skip
def test_an_agent_cannot_pin_send_accept_or_reply(host, argv):
    code, out = run(argv, {"ORCH_GRANT": "gr_x"})
    assert code == 3 and json.loads(out)["code"] == "op.not_allowed" and host.called == []


@pytest.mark.parametrize("argv", [["inbox"], ["list"], ["status", WS]])
def test_an_agent_may_read(host, argv):
    code, _ = run(argv, {"ORCH_GRANT": "gr_x"})
    assert code == 0 and len(host.called) == 1


def test_send_inbox_reply_shapes(host):
    host.reply["peer.send"] = {"envelope_id": "ab" * 16, "envelope_sha256": "cd" * 32, "state": "queued"}
    code, out = run(["send", WS, "--title", "T", "--body", "B"], {})
    assert code == 0 and json.loads(out)["data"]["envelope_sha256"] == "cd" * 32
    assert host.called[-1] == ("peer.send", {"to": WS, "title": "T", "body": "B"})
    host.reply["peer.inbox"] = {"items": [{"id": f"{WS}/{WS}", "from_ws": WS, "envelope_sha256": "cd" * 32}]}
    assert json.loads(run(["inbox"], {})[1])["data"]["items"][0]["from_ws"] == WS
    run(["reply", f"{WS}/{WS}", "--summary", "done"], {})
    assert host.called[-1] == ("peer.reply", {"id": f"{WS}/{WS}", "summary": "done"})


def test_via_hub_is_refused_honestly(host):
    code, out = run(["add", "--via-hub"], {})
    assert code == 5 and "card_get" in json.loads(out)["message"] and host.called == []


def test_a_hub_refusal_is_shown(host):
    host.reply["peer.send"] = OrchError("hub.refused", "the hub is refused: not_allowed")
    code, out = run(["send", WS, "--title", "T"], {})
    assert json.loads(out)["code"] == "hub.refused" and "not_allowed" in json.loads(out)["message"] and code != 0


def test_shape_errors(host):
    assert run(["send", WS], {})[0] == 2  # no --title
    assert run(["inbox", "x"], {})[0] == 2
    assert run(["frobnicate"], {})[0] == 2


def test_add_replace_flag_is_passed_and_an_agent_cannot_use_it(host, tmp_path):
    card = tmp_path / "c.json"
    card.write_text("{}")
    host.reply["peer.add"] = {"workspace_id": WS, "pinned": True, "how": "replaced"}
    code, _ = run(["add", "--card", str(card), "--replace"], {})
    assert code == 0 and host.called[-1] == ("peer.add", {"card": {}, "replace": True})
    code, out = run(["add", "--card", str(card), "--replace"], {"ORCH_GRANT": "g"})
    assert json.loads(out)["code"] == "op.not_allowed"
