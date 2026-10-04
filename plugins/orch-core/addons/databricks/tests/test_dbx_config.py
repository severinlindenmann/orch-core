import pytest

from orch_databricks.config import EnvSpec, owned, parse_envs, prefixes, scope_mode

HOST = "https://adb-3333333333333333.13.azuredatabricks.net"


def test_live_and_simulated_envs_keep_their_order():
    envs = parse_envs({"envs": {"dev": f"acme_test @ {HOST}/", "int": "simulated:int", "prod": "simulated:prod"}})
    assert envs == [EnvSpec("dev", "acme_test", HOST), EnvSpec("int", simulated="int"), EnvSpec("prod", simulated="prod")]
    assert envs[0].live and not envs[1].live


@pytest.mark.parametrize("value, needle", [
    ("acme_test", "pin the workspace host"),
    (f"DEFAULT @ {HOST}", "never used"),
    (f"default @ {HOST}", "never used"),
    (f"__settings__ @ {HOST}", "never used"),
    ("acme_test @ http://adb.example", "pin the workspace host"),
    ("simulated:", "fixture name"),
    ("simulated:../../etc", "fixture name"),
    ("--profile x @ https://a.b", "write dev = <profile>"),
])
def test_env_problems(value, needle):
    [env] = parse_envs({"envs": {"dev": value}})
    assert env.problem and needle in env.problem and not env.live


def test_bad_env_name():
    [env] = parse_envs({"envs": {"Dev Env": "simulated:int"}})
    assert "lowercase" in env.problem


def test_no_mapping_means_no_guess():
    assert parse_envs({}) == [] and parse_envs({"envs": "dev = acme_test"}) == []


def test_at_most_ten_envs():
    assert len(parse_envs({"envs": {f"e{i}": "simulated:int" for i in range(15)}})) == 10


def test_scope_and_prefixes():
    assert scope_mode({}) == "mine" and scope_mode({"scope": "everyone"}) == "mine" and scope_mode({"scope": "all"}) == "all"
    assert prefixes({"prefixes": "[dev severin] , acme-, "}) == ("[dev severin]", "acme-")
    assert prefixes({}) == ()


def test_owned():
    item = {"name": "[dev severin] ingest", "owners": ["Severin@Example.com", None]}
    assert owned(item, "mine", "severin@example.com", ())
    assert not owned(item, "mine", "other@example.com", ()) and not owned(item, "mine", None, ())
    assert owned(item, "prefix", None, ("[dev severin]",)) and not owned(item, "prefix", None, ("acme-",))
    assert not owned(item, "prefix", None, ())
    assert owned({"name": "x"}, "all", None, ())
