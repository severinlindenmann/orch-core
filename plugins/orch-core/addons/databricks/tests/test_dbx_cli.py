import pytest

from orch.testing import FakeRunner
from orch_databricks.config import EnvSpec
from orch_databricks.dbcfg import host_problem, profiles
from orch_databricks.dbcli import READ_ONLY, DbxError, classify, login_command, run_json

HOST = "https://adb-3333333333333333.13.azuredatabricks.net"
ENV = EnvSpec("dev", "acme_test", HOST)
CFG = (f"[DEFAULT]\n[__settings__]\nauth_storage = secure\n[acme_test]\nhost = {HOST}/\nauth_type = databricks-cli\n"
       "[other]\nhost = https://adb-1.azuredatabricks.net\n")
ME = ["databricks", "current-user", "me", "--profile", "acme_test", "-o", "json"]
EXPIRED = ("Error: default auth: databricks-cli: cannot get access token: oauth2: \"invalid_grant\" \"Refresh token is "
           "invalid\". Try logging in again with `databricks auth login --profile acme_test` before retrying.\n")


class Ctx:
    """The one ProviderContext method dbcli uses."""

    def __init__(self, runner):
        self.runner = runner

    def run(self, argv, timeout=20.0):
        return self.runner(list(argv), timeout)


@pytest.fixture
def cfg(home):
    (home / ".databrickscfg").write_text(CFG, encoding="utf-8")
    return home


def test_allowlist_is_exactly_the_read_only_commands():
    assert READ_ONLY == {("current-user", "me"), ("auth", "describe"), ("jobs", "list-runs"),
                         ("pipelines", "list-pipelines"), ("warehouses", "list"), ("clusters", "list"),
                         ("clusters", "get")}
    writes = {"create", "delete", "edit", "start", "restart", "run-now", "submit", "deploy", "destroy", "run",
              "token", "update", "reset", "cancel-run", "permanent-delete", "stop", "login", "logout"}
    assert not {verb for _, verb in READ_ONLY} & writes


def test_no_bundle_command_is_ever_allowed():
    """databricks bundle * loads databricks.yml, which runs the repo's scripts and Python (preinit/postinit):
    no bundle command is read-only."""
    assert not [c for c in READ_ONLY if c[0] == "bundle"]


def test_call_names_the_profile_and_asks_for_json(cfg):
    runner = FakeRunner().add(ME, stdout_json={"userName": "me@example.com"})
    assert run_json(Ctx(runner), ENV, ("current-user", "me")) == {"userName": "me@example.com"}
    assert runner.calls == [tuple(ME)]


def test_a_command_off_the_list_never_runs(cfg):
    runner = FakeRunner()
    with pytest.raises(DbxError, match="read-only list"):
        run_json(Ctx(runner), ENV, ("jobs", "run-now"), "--job-id", "1")
    assert runner.calls == []


@pytest.mark.parametrize("cfg_text, needle", [
    (None, "not in ~/.databrickscfg"),
    ("", "not in ~/.databrickscfg"),
    (CFG.replace(HOST + "/", "https://adb-999.azuredatabricks.net"), "now points to https://adb-999.azuredatabricks.net"),
])
def test_host_is_checked_before_every_call(home, cfg_text, needle):
    if cfg_text is not None:
        (home / ".databrickscfg").write_text(cfg_text, encoding="utf-8")
    runner = FakeRunner()
    with pytest.raises(DbxError) as e:
        run_json(Ctx(runner), ENV, ("current-user", "me"))
    assert e.value.kind == "host" and needle in e.value.message and runner.calls == []


@pytest.mark.parametrize("rec, kind, needle", [
    ({"returncode": 1, "stderr": EXPIRED}, "auth_required", "invalid_grant"),
    ({"returncode": 1, "stderr": 'Error: Get "https://adb-1": dial tcp: lookup adb-1: no such host\n'}, "offline", "no such host"),
    ({"returncode": 1, "stderr": "Error: something else\nmore"}, "error", "something else"),
    ({"returncode": 2, "stderr": ""}, "error", "exited with 2"),
    ({"raises": "missing"}, "error", "not installed"),
    ({"raises": "timeout"}, "offline", "timed out"),
    ({"returncode": 0, "stdout": "not json"}, "error", "did not return JSON"),
])
def test_failures_are_classified(cfg, rec, kind, needle):
    runner = FakeRunner([{"argv": ME, **rec}])
    with pytest.raises(DbxError) as e:
        run_json(Ctx(runner), ENV, ("current-user", "me"))
    assert e.value.kind == kind and needle in e.value.message


def test_empty_output_is_none(cfg):
    assert run_json(Ctx(FakeRunner().add(ME, stdout="  ")), ENV, ("current-user", "me")) is None


def test_profiles_skip_default_and_settings(cfg):
    assert profiles() == {"acme_test": HOST, "other": "https://adb-1.azuredatabricks.net"}
    assert host_problem("acme_test", HOST) is None


def test_unreadable_cfg_has_no_profiles(home):
    (home / ".databrickscfg").write_bytes(b"\xff\xfe[x")
    assert profiles() == {}


def test_classify_and_login_command():
    assert classify("HTTP 401 Unauthorized") == "auth_required" and classify("") == "error"
    assert login_command(ENV) == f"databricks auth login --host {HOST} --profile acme_test"
