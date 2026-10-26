"""Run 8 (owner 1.8): setup can set up the records history: the same code as ``orch records init`` and local
auto-commit on (never push), consented in the review. A failure is a retry item, never a failed setup."""
# ruff: noqa: F811 - the records world fixtures (w, home) are imported, then named as test arguments

import pytest

from orch.setup import steps
from orch.setup.protocol import SetupError
from orch.setup.review import RECORDS_TEXT
from tests.host.test_records_sync import home, w  # noqa: F401 - fixtures

from .conftest import post
from .fakes import FakePorts, names
from .test_create import AGENT, fake_signer, run_ok, setup_flow  # noqa: F401 - fake_signer: the signing stand-ins
from .test_secrets import reveal, words_for

WITH_RECORDS = {**AGENT, "records": True}


def test_records_must_be_a_boolean_and_absent_means_no(flow):
    with pytest.raises(SetupError) as e:
        post(flow, "profile", {**AGENT, "records": "yes"})
    assert e.value.extra["field"] == "records"
    assert post(flow, "profile", AGENT)[1] == {"ok": True}


def test_the_review_names_the_records_consent_and_binds_it_to_the_digest(tmp):
    f = setup_flow(tmp, FakePorts())
    post(f, "profile", WITH_RECORDS)
    r = reveal(f)
    post(f, "secrets/confirm", {"words": words_for(r)})
    rv = post(f, "review")[1]
    flag = next(x for x in rv["flags"] if x["id"] == "records")
    assert flag["text"] == RECORDS_TEXT
    assert "orch records" not in flag["text"] and "git" not in flag["text"].lower() and "never uploaded" in flag["text"]
    g = setup_flow(tmp, FakePorts())
    post(g, "profile", AGENT)
    r2 = reveal(g)
    post(g, "secrets/confirm", {"words": words_for(r2)})
    assert post(g, "review")[1]["digest"] != rv["digest"]


def test_the_run_sets_up_records_last_and_only_when_consented(tmp):
    f, ports, p, rv = run_ok(tmp, WITH_RECORDS)
    assert p["error"] is None and p["done"] is True
    assert [x for x in ports.log if x[0] == "records_setup"] == [("records_setup", ports.log[-1][1], str(tmp.work))]
    assert [s["id"] for s in p["steps"]][-1] == "records"
    assert next(s for s in p["steps"] if s["id"] == "records")["status"] == "done"
    f, ports, p, rv = run_ok(tmp, AGENT)
    assert "records_setup" not in names(ports) and all(x["id"] != "records" for x in rv["flags"])


def test_a_records_failure_is_a_retry_item_with_the_command_and_setup_still_finishes(tmp):
    ports = FakePorts()
    ports.fail_records = True
    f, ports, p, _ = run_ok(tmp, WITH_RECORDS, ports=ports)
    assert p["done"] is True and p["error"] is None
    assert "records" in p["retry"] and "orch records init" in p["reasons"]["records"]
    assert next(s for s in p["steps"] if s["id"] == "records")["status"] == "failed"


def test_the_real_port_runs_records_init_then_auto_on_without_push(monkeypatch, tmp_path):
    from orch.records import cli as records_cli
    from orch.setup.steps import RealPorts

    seen = []

    def main(argv, env, out, **seams):
        seen.append((list(argv), seams["confirm"]("ask")))
        out.write('{"ok": true}\n')
        return 0

    monkeypatch.setattr(records_cli, "main", main)
    RealPorts({}, tmp_path).records_setup("w" * 32, tmp_path)
    assert [a for a, _ in seen] == [
        ["init", "--workspace", "w" * 32, "--json"],
        ["auto", "on", "--workspace", "w" * 32, "--json"],
    ]
    assert all(consent is True for _, consent in seen) and not any("--push" in a for a, _ in seen)

    def refuse(argv, env, out, **seams):
        out.write('{"ok": false, "code": "records.not_dedicated", "message": "the record root is not dedicated"}\n')
        return 3

    monkeypatch.setattr(records_cli, "main", refuse)
    with pytest.raises(SetupError) as e:
        RealPorts({}, tmp_path).records_setup("w" * 32, tmp_path)
    assert "not dedicated" in str(e.value)


def test_the_real_port_in_a_records_root_nested_in_the_project_repo_is_a_retry_item_without_a_timer(
    monkeypatch,
    tmp_path,
    w,
    home,
):
    """Opus F2: the records root (the store) sits inside the project's own git repository. ``init`` refuses it (the
    repository must be dedicated), the step is a retry item, setup does not fail, and no auto-commit timer or
    auto entry is written. Real RealPorts, real records tool and real git; only the host link and the timer seam
    are the test's own."""
    import functools

    from orch.records import cli as records_cli
    from tests.host.test_records_part2 import FakeTimer
    from tests.host.test_records_sync import Env, git

    r = Env(w, tmp_path, home)
    git(r.root, "init", "-q")  # the project is a git repository: the store is nested inside it
    r.store.mkdir(exist_ok=True)
    (r.store / ".git").write_text("gitdir: ../.git\n")  # and its history would land in the PROJECT's repository
    timer = FakeTimer()
    monkeypatch.setattr(
        records_cli,
        "main",
        functools.partial(
            records_cli.main, link=r.link, root=r.root, workspace_id=r.w.wid, tips=r.tips, timer=timer,
            state=r.tips.path.parent,
        ),
    )  # fmt: skip
    ports = steps.RealPorts(r.env, tmp_path / "state")
    with pytest.raises(SetupError) as e:
        ports.records_setup(r.w.wid, r.store)
    assert e.value.code == "records.setup_failed"
    # the run turns that error into a retry item and finishes (the FakePorts test above pins that mapping)
    assert timer.calls == [] and r.tips.autos() == {}
    assert (r.store / ".git").read_text() == "gitdir: ../.git\n"  # untouched
    for unit in (".config/systemd/user", "Library/LaunchAgents"):
        assert not (r.home / unit).exists()
