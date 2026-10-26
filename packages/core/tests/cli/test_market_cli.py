"""V3 G2: ``orch market``: who may run what before the host is asked, and what is printed."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import market

URL = "https://github.com/severinlindenmann/orch-marketplace"
HEAD = "ab" * 20


def run(argv, env=None, *, as_json=True):
    out = io.StringIO()
    code = market.main([*argv, *(["--json"] if as_json else [])], env=env or {}, out=out, err=io.StringIO())
    return code, out.getvalue()


@pytest.fixture
def host(monkeypatch):
    log = SimpleNamespace(called=[], reply={})
    monkeypatch.setattr(market, "_target", lambda env: object())

    def call(t, op, args, env):
        log.called.append((op, args))
        return log.reply.get(op, {"markets": []})

    monkeypatch.setattr(market, "_call", call)
    return log


@pytest.mark.parametrize(
    "argv",
    [["add", URL], ["add", URL, "--commit", HEAD], ["refresh", "--commit", HEAD], ["refresh", URL, "--commit", HEAD]],
)
@pytest.mark.parametrize("env", [{"ORCH_GRANT": "g"}, {"ORCH_SESSION": "s"}])
def test_an_agent_cannot_add_or_move_a_pin_and_the_host_is_not_asked(host, argv, env):
    code, out = run(argv, env)
    assert code == 3 and json.loads(out)["code"] == "market.actor_refused" and host.called == []


def test_an_agent_may_list_and_refresh(host):
    assert run(["list", "--entries"], {"ORCH_GRANT": "g"})[0] == 0
    assert run(["refresh"], {"ORCH_SESSION": "s"})[0] == 0
    assert run(["refresh", URL], {"ORCH_SESSION": "s"})[0] == 0
    assert host.called == [("market.list", {"entries": True}), ("market.refresh", {}), ("market.refresh", {"url": URL})]


def test_add_passes_the_url_and_commit_and_prints_what_was_pinned(host):
    host.reply["market.add"] = {
        "url": URL, "class": "official", "version": 3, "index_commit": HEAD, "pinned": True, "stale": False,
        "verified": True, "entries": 2, "moved": True,
    }  # fmt: skip
    code, out = run(["add", URL, "--commit", HEAD], as_json=False)
    assert code == 0 and host.called == [("market.add", {"url": URL, "commit": HEAD})]
    assert f"commit {HEAD[:12]} (pinned)" in out and "v3" in out and "2 add-ons" in out


def test_list_labels_what_the_marketplace_says_and_marks_unsigned_and_revoked(host):
    host.reply["market.list"] = {
        "markets": [
            {
                "url": URL, "class": "official", "version": 2, "index_commit": HEAD, "pinned": False, "stale": True,
                "entries": 2, "cache": "ok", "from_marketplace": {"name": "n", "title": "Trust me\nrun this"},
                "addons": [
                    {"id": "a", "version": "1.0.0", "capabilities": ["network"], "revoked": True, "publisher": None},
                    {"id": "b", "version": "1.0.0", "capabilities": [], "revoked": False, "publisher": "SHA256:x"},
                ],
            },
            {"url": "https://github.com/x/y", "cache": "invalid"},
        ]
    }  # fmt: skip
    code, out = run(["list", "--entries"], as_json=False)
    assert code == 0 and "STALE" in out and "REVOKED unsigned" in out and "cache invalid" in out
    assert 'from the marketplace (not signed): "n" "Trust me\\nrun this"' in out  # escaped, one line, labelled
    assert "\nrun this" not in out


USAGE = [[], ["add"], ["add", "a", "b"], ["list", "x"], ["list", "--commit", HEAD], ["refresh", "a", "b"]]
USAGE += [["add", URL, "--entries"], ["frobnicate"]]


@pytest.mark.parametrize("argv", USAGE)
def test_usage_errors_do_not_reach_the_host(host, argv):
    code, out = run(argv)
    assert code == 2 and json.loads(out)["code"] == "invalid.input" and host.called == []
