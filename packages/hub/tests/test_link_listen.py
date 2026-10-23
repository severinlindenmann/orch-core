"""D12b item 1: ``--link-listen`` (the only non-loopback bind) and its refusals (design check G1-G3)."""

from __future__ import annotations

import io
import socket
import threading
import time

import pytest

from orch_hub import colocation as CO
from orch_hub.cli import main
from orch_hub.colocation import Accounts, BindRefused, check_bind_allowed, parse_link_listen
from orch_hub.keys import HubKeys
from orch_hub.serve import serve

ALWAYS = lambda ip: True


def _acc(*, hosts=("orch-host-ws2",), agents=("agent-ws2",), groups=None):
    users = {"orch-hub-split": (900, 900), "orch-hub": (986, 980)}
    for i, n in enumerate((*hosts, *agents)):
        users[n] = (910 + i, 910 + i)
    return Accounts(users, groups or {})


@pytest.mark.parametrize(
    "value,needle",
    [
        ("0.0.0.0:8106", "wildcard"),
        ("[::]:8106", "wildcard"),
        ("224.0.0.1:8106", "multicast"),
        ("255.255.255.255:8106", "multicast and broadcast"),
        ("169.254.1.1:8106", "link-local"),
        ("example.com:8106", "literal IP"),
        ("localhost:8106", "literal IP"),
        ("127.0.0.1:8106", "non-loopback"),
        ("[::1]:8106", "non-loopback"),
        ("8.8.8.8:8106", "public address"),
        ("[2001:db8::1]:8106", "public address"),
        ("100.63.255.255:8106", "public address"),
        ("100.71.244.99", "IP:port"),
        ("100.71.244.99:0", "IP:port"),
        ("100.71.244.99:70000", "IP:port"),
    ],
)
def test_address_refusals(value, needle):
    with pytest.raises(BindRefused, match=needle):
        parse_link_listen(value, assigned=ALWAYS)


@pytest.mark.parametrize(
    "value",
    [
        "100.71.244.99:8106",
        "10.1.2.3:1",
        "172.16.0.9:65535",
        "192.168.1.5:8106",
        "100.127.255.255:8106",
        "[fd00::5]:8106",
    ],
)
def test_private_unicast_is_accepted(value):
    host, port = parse_link_listen(value, assigned=ALWAYS)
    assert host == value.rpartition(":")[0].strip("[]") and port == int(value.rpartition(":")[2])


def test_unassigned_address_is_refused():
    with pytest.raises(BindRefused, match="not assigned"):
        parse_link_listen("10.254.254.254:8106", assigned=lambda ip: False)
    assert CO.locally_assigned("127.0.0.1") and not CO.locally_assigned("203.0.113.77")


def test_clean_machine_is_allowed_and_the_flag_there_is_an_error():
    acc = Accounts({"orch-hub": (986, 980), "orch-relay": (995, 987)}, {})
    assert check_bind_allowed(acc, hub_user="orch-hub", card_dev=False, dev_shared_machine=False) is None
    with pytest.raises(BindRefused, match="no host or agent users exist"):
        check_bind_allowed(acc, hub_user="orch-hub", card_dev=True, dev_shared_machine=True)


@pytest.mark.parametrize("acc", [_acc(), _acc(agents=()), _acc(hosts=())])
def test_host_or_agent_users_refuse_without_the_flag(acc):
    with pytest.raises(BindRefused, match="split layout"):
        check_bind_allowed(acc, hub_user="orch-hub-split", card_dev=True, dev_shared_machine=False)


def test_dev_flag_with_a_non_dev_card_is_a_hard_error():
    with pytest.raises(BindRefused, match="dev: true"):
        check_bind_allowed(_acc(), hub_user="orch-hub-split", card_dev=False, dev_shared_machine=True)


def test_dev_exception_warns_and_checks_the_hub_user():
    warn = check_bind_allowed(_acc(), hub_user="orch-hub-split", card_dev=True, dev_shared_machine=True)
    assert warn and "not machine separation" in warn
    with pytest.raises(BindRefused, match="a host or agent user"):
        check_bind_allowed(_acc(), hub_user="agent-ws2", card_dev=True, dev_shared_machine=True)
    with pytest.raises(BindRefused, match="shares a group"):  # supplementary membership of an agent group
        check_bind_allowed(
            _acc(groups={911: frozenset({"orch-hub-split"})}),
            hub_user="orch-hub-split",
            card_dev=True,
            dev_shared_machine=True,
        )
    with pytest.raises(BindRefused, match="no account entry"):
        check_bind_allowed(_acc(), hub_user="ghost", card_dev=True, dev_shared_machine=True)


def test_extra_flag_named_users_count():
    acc = Accounts({"orch-hub": (986, 980), "ws-alpha": (900, 900)}, {}, frozenset({"ws-alpha"}))
    with pytest.raises(BindRefused, match="ws-alpha"):
        check_bind_allowed(acc, hub_user="orch-hub", card_dev=True, dev_shared_machine=False)


# ---- serve() wiring -------------------------------------------------------------------------------------------


def _private_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.254", 9))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    try:
        parse_link_listen(f"{ip}:1")
    except BindRefused:
        return None
    return ip


def _free_port(ip):
    s = socket.socket()
    s.bind((ip, 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "hub"
    HubKeys.init(s)
    return s


def _serve(state, **kw):
    kw.setdefault("accounts", Accounts({"orch-hub": (986, 980)}, {}))
    return serve(state, kw.pop("listen", None), "http://127.0.0.1:9", [], **kw)


def test_serve_needs_exactly_one_listen(state):
    with pytest.raises(ValueError, match="exactly one"):
        _serve(state)
    with pytest.raises(ValueError, match="exactly one"):
        _serve(state, listen="127.0.0.1:0", link_listen="10.1.2.3:8106", assigned=ALWAYS)
    with pytest.raises(ValueError, match="only goes with --link-listen"):
        _serve(state, listen="127.0.0.1:0", dev_shared_machine=True)


def test_serve_refuses_a_loopback_flag_value_on_listen_and_a_wildcard_on_link_listen(state):
    with pytest.raises(ValueError, match="loopback"):
        _serve(state, listen="10.1.2.3:8106")  # --listen stays loopback-only
    with pytest.raises(BindRefused, match="wildcard"):
        _serve(state, link_listen="0.0.0.0:8106", assigned=ALWAYS)


def test_serve_refuses_host_users_and_dev_flag_on_a_non_dev_card(state, tmp_path, monkeypatch):
    with pytest.raises(BindRefused, match="split layout"):
        _serve(state, link_listen="10.1.2.3:8106", assigned=ALWAYS, accounts=_acc())
    from orch_hub.keys import store

    monkeypatch.setattr(store, "DEV", False)  # V3 hubs are all dev; a prod card is made by flipping the one constant
    nondev = tmp_path / "prod"
    HubKeys.init(nondev)
    with pytest.raises(BindRefused, match="dev: true"):
        _serve(nondev, link_listen="10.1.2.3:8106", assigned=ALWAYS, accounts=_acc(), dev_shared_machine=True)


def test_cli_reports_a_refusal(state, capsys):
    rc = main(["serve", "--state", str(state), "--link-listen", "0.0.0.0:8106", "--publish-url", "http://127.0.0.1:9"])
    assert rc == 1
    assert "refused: --link-listen refuses a wildcard" in capsys.readouterr().err


@pytest.mark.skipif(_private_local_ip() is None, reason="no private local address on this machine")
@pytest.mark.parametrize("shared", [False, True])
def test_accepted_bind_serves_and_prints_the_warn_only_for_the_exception(state, shared):
    ip = _private_local_ip()
    stop, out, result = threading.Event(), io.StringIO(), []
    acc = _acc() if shared else Accounts({"orch-hub": (986, 980)}, {})
    me = CO.current_user(acc)
    if shared:  # run as a user that is not a host/agent user and in none of their groups
        acc = Accounts({**acc.users, me: (CO.os.geteuid(), 5000)}, acc.groups)
    t = threading.Thread(
        target=lambda: result.append(
            serve(
                state,
                None,
                "http://127.0.0.1:9",
                [],
                link_listen=f"{ip}:{_free_port(ip)}",
                dev_shared_machine=shared,
                accounts=acc,
                stop=stop,
                out=out,
            )
        ),
        daemon=True,
    )
    t.start()
    end = time.monotonic() + 10
    while "orch-hub ready" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.05)
    try:
        assert "orch-hub ready" in out.getvalue(), out.getvalue()
        assert ("WARN dev split on a shared machine" in out.getvalue()) is shared
    finally:
        stop.set()
        t.join(15)
    assert result == [0]
