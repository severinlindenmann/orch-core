"""#37: agents report orch friction into a local, redacted queue; only the human files it as an orch-core issue."""
import json
import subprocess

import pytest

from orch import feedback
from orch.cli import run


@pytest.fixture
def human(monkeypatch):
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)


@pytest.fixture
def as_agent(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")


def _reports():
    return feedback.reports()


def test_add_stores_a_redacted_report_outside_the_repo(configure, ws_root, as_agent, capsys):
    ws = configure(customer="Initech Bank", git={"repos": {"payments-core": {}}},
                   external_trackers=[{"prefix": "PAY", "pattern": "PAY-\\d+", "url": "https://jira.initech.example/browse/{key}"}])
    from orch.core.ops import Ops
    from orch.core.events import Actor
    Ops(ws, Actor("agent", "claude-code", "cli", "s1")).new(title="Migrate the Initech ledger to Kafka")
    problem = (f"orch move L-0001 testing refused in {ws_root}/payments-core for PAY-1234 at Initech Bank; "
               "see https://jira.initech.example/browse/PAY-1234, mail bob@initech.example, token "
               "ghp_abcdefghijklmnopqrstuvwxyz0123456789; title was Migrate the Initech ledger to Kafka")
    assert run(["feedback", "add", "--command", f"orch move L-0001 testing --dir {ws_root}", "-m", problem]) == 0
    out = capsys.readouterr().out
    assert "orch feedback" in out and "carry on" in out.lower()
    [r] = _reports()
    text = json.dumps(r)
    for secret in ("Initech", "initech", "payments-core", "PAY-1234", str(ws_root), "bob@", "ghp_", "Kafka", "L-0001"):
        assert secret not in text, secret
    assert "<customer>" in r["problem"] and "<workspace>" in r["problem"] and "<url>" in r["problem"]
    assert r["orch_version"] and r["harness"] == "claude-code" and r["count"] == 1 and r["status"] == "open"
    assert feedback.feedback_dir().is_relative_to(ws_root) is False
    assert not any("feedback" in p.name for p in (ws_root / "orchestrator").rglob("*"))


def test_same_report_is_counted_not_duplicated(ws, as_agent, capsys):
    for _ in range(2):
        assert run(["feedback", "add", "--command", "orch wait L-0001", "-m", "wait never returns"]) == 0
    [r] = _reports()
    assert r["count"] == 2
    assert "already reported" in capsys.readouterr().out


def test_rate_limit_per_workspace_and_day(ws, as_agent, capsys):
    for i in range(feedback.DAILY_LIMIT + 2):
        assert run(["feedback", "add", "--command", f"orch thing{i}", "-m", f"problem {i}"]) == 0
    assert len(_reports()) == feedback.DAILY_LIMIT
    assert "limit" in capsys.readouterr().out


def test_switched_off_per_workspace(configure, as_agent, capsys):
    configure(feedback={"enabled": False})
    assert run(["feedback", "add", "--command", "orch x", "-m", "y"]) == 0
    assert _reports() == [] and "turned off" in capsys.readouterr().out


def test_add_works_outside_a_workspace(tmp_path, monkeypatch, as_agent):
    monkeypatch.chdir(tmp_path)
    assert run(["feedback", "add", "--command", f"orch init --dir {tmp_path}", "-m", "init failed"]) == 0
    [r] = _reports()
    assert str(tmp_path) not in json.dumps(r)


def test_long_text_is_capped(ws, as_agent):
    assert run(["feedback", "add", "--command", "orch x", "-m", "a " * 5000]) == 0
    [r] = _reports()
    assert len(r["problem"]) <= feedback.MAX_PROBLEM + 1


@pytest.mark.parametrize("args", [["list"], ["show", "fb-x"], ["file", "fb-x"], ["dismiss", "fb-x"]])
def test_reading_and_filing_are_human_only(ws, as_agent, args):
    assert run(["feedback", *args]) != 0


def test_list_and_show(ws, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    run(["feedback", "add", "--command", "orch wait L-1", "-m", "never returns"])
    monkeypatch.delenv("CLAUDECODE")
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    capsys.readouterr()
    assert run(["feedback", "list"]) == 0
    rid = _reports()[0]["id"]
    assert rid in capsys.readouterr().out
    assert run(["feedback", "show", rid]) == 0
    out = capsys.readouterr().out
    assert "never returns" in out and "## What happened" in out


def test_file_shows_the_exact_text_and_needs_a_typed_yes(ws, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    run(["feedback", "add", "--command", "orch wait L-1", "-m", "never returns"])
    monkeypatch.delenv("CLAUDECODE")
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    rid = _reports()[0]["id"]
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, open(argv[argv.index("--body-file") + 1], encoding="utf-8").read()))
        return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/o/r/issues/99\n", stderr="")

    monkeypatch.setattr(feedback.subprocess, "run", fake_run)
    monkeypatch.setattr(feedback.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert run(["feedback", "file", rid]) != 0
    assert calls == [] and _reports()[0]["status"] == "open"
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt="": "FILE")
    assert run(["feedback", "file", rid, "--note", "seen in the Initech workspace"]) == 0
    out = capsys.readouterr().out
    [(argv, body)] = calls
    assert argv[:3] == ["gh", "issue", "create"] and argv[argv.index("-R") + 1] == feedback.REPO
    assert body in out and "never returns" in body and "seen in the Initech workspace" in body
    r = _reports()[0]
    assert r["status"] == "filed" and r["filed_url"] == "https://github.com/o/r/issues/99"


def test_dismiss(ws, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    run(["feedback", "add", "--command", "orch x", "-m", "y"])
    monkeypatch.delenv("CLAUDECODE")
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    rid = _reports()[0]["id"]
    assert run(["feedback", "dismiss", rid]) == 0
    assert _reports()[0]["status"] == "dismissed"


def test_agents_rule_mentions_feedback_and_can_be_switched_off():
    from orch.config.load import DEFAULTS, deep_merge
    from orch.instructions.render import agents_rules
    on = agents_rules(deep_merge(DEFAULTS, {}))
    assert "orch feedback add" in on and "never open an issue" in on.lower()
    off = agents_rules(deep_merge(DEFAULTS, {"feedback": {"enabled": False}}))
    assert "orch feedback" not in off


def test_guard_lets_agents_report_through_a_file(ws):
    """Inline text that quotes a human-only command is denied like any such command line (the guard cannot tell a
    quote from a call), so the rule tells agents to write the report into a file under temporary/ first."""
    from orch.hooks import guard
    report = ws.temporary_dir / "orch-feedback.md"
    write = {"tool_name": "Write", "cwd": str(ws.root),
             "tool_input": {"file_path": str(report), "content": "`orch approve L-0001 plan` was refused for me"}}
    assert guard.evaluate(ws, write).allow
    cmd = "orch feedback add --file orchestrator/temporary/orch-feedback.md"
    assert guard.evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow


def test_add_from_a_file(ws, as_agent):
    report = ws.temporary_dir / "orch-feedback.md"
    report.write_text("orch wait L-0001 never returns", encoding="utf-8")
    assert run(["feedback", "add", "--file", str(report)]) == 0
    [r] = _reports()
    assert r["problem"] == "orch wait <ticket> never returns"


def test_redact_keeps_workspace_relative_paths_only():
    text = feedback.redact("cat orchestrator/.state/gates/x.md and /etc/hosts and ~/clients/initech/ws and C:\\w\\initech")
    assert text.startswith("cat orchestrator/.state/gates/x.md and <path> and <path>")
    assert "initech" not in text


# -- fix round 1: leaks the review found ---------------------------------------------------------------------------

@pytest.fixture
def client_ws(configure):
    return configure(customer="Initrode Versicherungen", git={"repos": {"claims-etl": {}}})


@pytest.mark.parametrize("host", ["adb-1234567890123456.7.azuredatabricks.net", "dbc-1a2b3c4d.cloud.databricks.com",
                                  "1234.5.gcp.databricks.com", "jira.kundenportal.ch", "git.example-bank.de",
                                  "build.acme.io", "wiki.globex.org", "files.globex.cloud"])
def test_bare_hosts_are_redacted(host):
    text = feedback.redact(f"orch addon refresh failed: could not reach {host} (timeout)")
    assert host.split(".")[-2] not in text and "<host>" in text


def test_ordinary_dotted_words_survive():
    text = feedback.redact("cat orchestrator/.state/events.jsonl and config.json; orch 0.4.1 exit 3")
    assert "events.jsonl" in text and "config.json" in text and "0.4.1" in text


@pytest.mark.parametrize("raw", ["initrode_prod.claims.policies", "INITRODE-dev", "claims_etl failed",
                                 "the claims etl repo", "CLAIMS-ETL", "versicherungen_raw"])
def test_customer_and_repo_variants_are_redacted(client_ws, raw):
    text = feedback.redact(f"orch query on {raw} broke", client_ws).lower()
    assert "initrode" not in text and "claims_etl" not in text and "claims-etl" not in text
    assert "claims etl" not in text and "versicherungen" not in text


@pytest.mark.parametrize("raw, gone", [("token=abc123", "abc123"), ("password: hunter2", "hunter2"),
                                       ("api_key=Zx9", "Zx9"), ("Authorization: Bearer abc.def", "abc.def"),
                                       ("pwd=s3cr3t", "s3cr3t"), ("secret = shh", "shh"),
                                       ("ghp_short1", "ghp_short1"), ("github_pat_11AB", "github_pat_11AB"),
                                       ("dapi0123abcd", "dapi0123abcd"), ("dose0123abcd", "dose0123abcd"),
                                       ("AKIAABCDEFGH", "AKIAABCDEFGH"), ("xoxb-123-456", "xoxb-123-456"),
                                       ("sk-abc123", "sk-abc123"), ("glpat-abc123", "glpat-abc123")])
def test_secrets_are_redacted(raw, gone):
    assert gone not in feedback.redact(f"orch addon check printed {raw} then stopped")


def test_jwts_are_redacted_whole():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig-part_x"
    text = feedback.redact(f"header {jwt} end")
    assert "eyJ" not in text and "sig-part_x" not in text and text == "header <redacted> end"


@pytest.mark.parametrize("raw", ["\\\\fileserver\\initrode\\ws", "C:\\Users\\bob\\initrode", "d:\\work\\initrode"])
def test_windows_paths_are_redacted(raw):
    text = feedback.redact(f"orch init failed in {raw} today")
    assert "initrode" not in text and "fileserver" not in text and text == "orch init failed in <path> today"


def test_feedback_dir_is_private(ws, as_agent):
    import os
    import stat
    assert run(["feedback", "add", "--command", "orch x", "-m", "y"]) == 0
    if os.name == "posix":
        assert stat.S_IMODE(feedback.feedback_dir().stat().st_mode) == 0o700


def test_file_needs_exactly_FILE(ws, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    run(["feedback", "add", "--command", "orch x", "-m", "y"])
    monkeypatch.delenv("CLAUDECODE")
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr(feedback.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(feedback.subprocess, "run", lambda *a, **k: pytest.fail("must not file"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "file")
    assert run(["feedback", "file", _reports()[0]["id"]]) != 0
    assert _reports()[0]["status"] == "open"


def test_rule_says_to_delete_the_report_file():
    from orch.config.load import DEFAULTS
    from orch.instructions.render import agents_rules
    assert "then delete" in agents_rules(DEFAULTS)
