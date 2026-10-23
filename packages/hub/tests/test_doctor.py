"""``orch-hub doctor``: PASS/FAIL lines, exit code, no secrets in output."""

from __future__ import annotations

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from orch_hub import doctor as D
from orch_hub.cli import main
from orch_hub.doctor import Accounts, run
from orch_hub.keys import HubKeys
from orch_hub.link import LinkServer, Registry
from orch_hub.serve import REGISTRY_FILE, FixedInvites


@pytest.fixture(autouse=True)
def _no_remote_dials(monkeypatch):
    """Doctor dials only loopback in tests; a non-loopback address (the split hub) must not wait for a timeout."""
    real = D.tls.connect_pinned

    def dial(host, port, pin, **kw):
        if host != "127.0.0.1":
            raise D.tls.LinkClosed("test: no route")
        return real(host, port, pin, **kw)

    monkeypatch.setattr(D.tls, "connect_pinned", dial)


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "hub"
    HubKeys.init(s)
    return s


@pytest.fixture
def publish():
    box = {"status": 400, "body": b'{"error":"malformed"}'}

    class H(BaseHTTPRequestHandler):
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(box["status"])
            self.send_header("content-length", str(len(box["body"])))
            self.end_headers()
            self.wfile.write(box["body"])

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    box["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield box
    srv.shutdown()
    srv.server_close()


def doctor(capsys, state, *extra):
    rc = main(["doctor", "--state", str(state), *extra])
    return rc, capsys.readouterr().out


def test_all_pass_with_listen_and_publish(state, publish, capsys):
    keys = HubKeys.open(state)
    reg = Registry(str(state / REGISTRY_FILE))
    os.chmod(state / REGISTRY_FILE, 0o600)
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True).start()
    try:
        rc, out = doctor(capsys, state, "--listen", f"127.0.0.1:{srv.address[1]}", "--publish-url", publish["url"])
    finally:
        srv.close()
        reg.close()
    assert rc == 0, out
    assert "FAIL" not in out
    assert out.count("PASS ") == 7
    assert "doctor: all checks passed" in out


def test_loose_state_dir_fails(state, capsys):
    os.chmod(state, 0o755)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL state dir and key file modes" in out
    assert "FAIL key store opens" in out


def test_loose_key_file_fails_and_no_key_bytes_in_output(state, capsys):
    key = state / "hk_root.key"
    secret = key.read_bytes().hex()
    os.chmod(key, 0o644)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "hk_root.key must be 0600" in out
    assert secret not in out


def test_missing_state_fails(tmp_path, capsys):
    rc, out = doctor(capsys, tmp_path / "nope")
    assert rc == 1
    assert "FAIL key store opens" in out


def test_non_dev_card_fails(state, capsys, monkeypatch):
    monkeypatch.setattr(HubKeys, "verify_card", lambda self, c: {"card": dict(c["o"], dev=False)})
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL card verifies and is dev: true: card is not dev: true" in out


def test_tls_pin_mismatch_fails(state, capsys):
    keys = HubKeys.open(state)
    reg = Registry()
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True).start()
    try:
        keys.reissue_card(tls_spki_sha256="A" * 43)  # the card now names another key than the server presents
        rc, out = doctor(capsys, state, "--listen", f"127.0.0.1:{srv.address[1]}")
    finally:
        srv.close()
        reg.close()
    assert rc == 1
    assert "FAIL TLS pin matches the served key: served TLS key does not match" in out


def test_non_loopback_listen_is_refused(state, capsys):
    rc, out = doctor(capsys, state, "--listen", "0.0.0.0:1")
    assert rc == 1
    assert "FAIL TLS pin" in out


@pytest.mark.parametrize(
    "status,body",
    [(200, b'{"ok":true}'), (404, b"{}"), (400, b'{"error":"bad_sig"}'), (400, b"not json")],
)
def test_publish_route_wrong_answer_fails(state, publish, capsys, status, body):
    publish.update(status=status, body=body)
    rc, out = doctor(capsys, state, "--publish-url", publish["url"])
    assert rc == 1
    assert "FAIL publish URL answers /v2/hub/card" in out


def test_publish_unreachable_fails(state, capsys):
    rc, out = doctor(capsys, state, "--publish-url", "http://127.0.0.1:1")
    assert rc == 1
    assert "publish URL not reachable" in out


def test_unreadable_registry_fails(state, capsys):
    (state / REGISTRY_FILE).write_bytes(b"not sqlite")
    os.chmod(state / REGISTRY_FILE, 0o600)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL registry readable" in out


def test_doctor_is_read_only_a_stray_generation_survives(state, capsys):
    stray = state / "op" / "99"
    stray.mkdir(mode=0o700)
    (stray / "key").write_bytes(b"x")
    os.chmod(stray / "key", 0o600)
    doctor(capsys, state)
    assert stray.is_dir() and (stray / "key").exists()
    HubKeys.open(state)  # the default open still prunes uncommitted generations
    assert not stray.exists()


# ---- §4a co-location checks (D12) -------------------------------------------------------------------------------

MISSING_FW = Path("/nonexistent/hub-fw.nft")
HUB_UID = 986


def _jrule(chain, *, iface=None, iface_key="oifname", port=8105, ids=None, verdict="reject"):
    expr = []
    if iface:
        expr.append({"match": {"op": "==", "left": {"meta": {"key": iface_key}}, "right": iface}})
    expr.append({"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": port}})
    if ids is not None:
        expr.append({"match": {"op": "!=", "left": {"meta": {"key": "skuid"}}, "right": {"set": sorted(ids)}}})
    expr.append({verdict: {}} if verdict != "reject" else {"reject": {"type": "tcp reset"}})
    return {"rule": {"family": "inet", "table": "orch_hub", "chain": chain, "expr": expr}}


def _dump(path, rules=None, *, hooks=(("out", "output"), ("in", "input"))):
    items = [{"metainfo": {"version": "1.1.3"}}, {"table": {"family": "inet", "name": "orch_hub"}}]
    items += [{"chain": {"table": "orch_hub", "name": n, "hook": h}} for n, h in hooks]
    items += rules if rules is not None else [_jrule("out", iface="lo", ids={0, 986, 992})]
    path.write_text(json.dumps({"nftables": items}))
    return path


def _acc(extra=None, *, groups=None, drop=()):
    users = {
        "orch-hub": (986, 980),
        "orch-relay": (995, 987),
        "orch-publish": (993, 985),
        "orch-host-ws2": (992, 984),
        "agent-ws2": (991, 983),
    }
    users.update(extra or {})
    for d in drop:
        users.pop(d)
    return Accounts(users, groups or {})


def _fw(tmp_path, ids='0, "orch-hub", "orch-host-ws2"', port=8105, verdict="reject", pre=""):
    f = tmp_path / "hub-fw.nft"
    f.write_text(
        f'{pre}table inet orch_hub {{ chain out {{ oifname "lo" tcp dport {port} meta skuid != {{ {ids} }} {verdict} }} }}'
    )
    return f


def _run(state, acc, fw, *, dump=None, listen="127.0.0.1:8105", unit="orch-hub", suid=HUB_UID, **kw):
    out = io.StringIO()
    if dump is None:  # a loaded table that agrees with the canonical file (when the file's directory exists)
        dump = _dump(fw.parent / "loaded.json") if fw.parent.is_dir() else fw.parent / "loaded.json"
    run(
        state,
        listen=listen,
        out=out,
        accounts=acc,
        fw_file=fw,
        fw_dump=dump,
        state_uid=suid,
        unit_user=lambda: unit,
        **kw,
    )
    return out.getvalue()


def test_colocation_good_layout_passes_and_is_reported(state, tmp_path):
    out = _run(state, _acc(), _fw(tmp_path))
    assert "INFO layout: one machine (hub + 1 workspace host(s), relay/publish)" in out
    for name in (
        "hub runs as its own OS user",
        "host and agent users are separate",
        "hosts use the server custody profile",
        "link firewall allows only",
    ):
        assert f"PASS {name}" in out, out


def test_split_layout_has_no_colocation_checks(state):
    out = _run(state, Accounts({"orch-hub": (986, 980)}, {}), MISSING_FW)
    assert "INFO layout: hub alone" in out
    assert "link firewall" not in out


def test_edge_only_layout_is_not_hub_alone(state, tmp_path):
    acc = _acc(drop=("orch-host-ws2", "agent-ws2"))
    out = _run(state, acc, MISSING_FW)
    assert "hub alone" not in out
    assert "INFO layout: one machine (hub + relay/publish)" in out
    assert "PASS hub runs as its own OS user" in out
    assert "link firewall" not in out  # no workspace host here: nothing dials the link locally


def test_layout_flags(state, tmp_path):
    acc = Accounts({"orch-hub": (986, 980)}, {})
    assert "INFO layout: one machine (hub + nothing found, forced by --layout)" in _run(
        state, acc, MISSING_FW, layout_flag="one-vps"
    )
    assert "FAIL layout: --layout split but" in _run(state, _acc(), _fw(tmp_path), layout_flag="split")


def test_unreadable_accounts_fail_closed(state, monkeypatch):
    def boom(*a):
        raise OSError("nss down")

    monkeypatch.setattr(Accounts, "load", classmethod(lambda cls, *a: boom()))
    out = io.StringIO()
    rc = run(state, out=out)
    assert rc >= 1
    assert "FAIL OS accounts readable: unexpected OSError" in out.getvalue()
    assert "FAIL layout: OS accounts unreadable, layout unknown" in out.getvalue()
    assert "hub alone" not in out.getvalue()


def test_flag_named_host_and_agent_users_are_found(state, tmp_path):
    acc = Accounts(
        {"orch-hub": (986, 980), "ws-alpha": (900, 900), "robot-alpha": (901, 901)},
        {},
        frozenset({"ws-alpha"}),
        frozenset({"robot-alpha"}),
    )
    out = _run(state, acc, MISSING_FW)
    assert "1 workspace host(s)" in out
    assert "FAIL hosts use the server custody profile: ws-alpha has no agent user agent-ws-alpha" in out
    assert "PASS host and agent users are separate" in out


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"suid": 992}, "state dir is owned by orch-host-ws2, expected orch-hub"),
        ({"suid": 4242}, "state dir is owned by uid 4242"),
        ({"unit": "root"}, "the hub unit runs as root, expected orch-hub"),
    ],
)
def test_hub_user_judged_by_state_owner_and_unit(state, tmp_path, kw, needle):
    out = _run(state, _acc(), _fw(tmp_path), **kw)
    assert f"FAIL hub runs as its own OS user: {needle}" in out, out


def test_unit_user_unknown_still_checks_state_owner(state, tmp_path):
    out = io.StringIO()
    run(
        state,
        listen="127.0.0.1:8105",
        out=out,
        accounts=_acc(),
        fw_file=_fw(tmp_path),
        state_uid=HUB_UID,
        unit_user=lambda: None,
        fw_dump=_dump(tmp_path / "loaded2.json"),
    )
    assert "PASS hub runs as its own OS user" in out.getvalue()


@pytest.mark.parametrize(
    "acc,needle",
    [
        (_acc({"agent-x": (986, 777)}), "orch-hub uid or primary group shared with agent-x"),
        (_acc(groups={980: frozenset({"agent-ws2"})}), "group orch-hub has other members: agent-ws2"),
    ],
)
def test_colocation_hub_user_failures(state, tmp_path, acc, needle):
    assert f"FAIL hub runs as its own OS user: {needle}" in _run(state, acc, _fw(tmp_path))


def test_host_or_agent_sharing_identity_fails(state, tmp_path):
    out = _run(state, _acc({"orch-host-ws3": (992, 900), "agent-ws3": (905, 905)}), _fw(tmp_path))
    assert "orch-host-ws3 and orch-host-ws2 share a uid" in out
    out = _run(state, _acc({"agent-ws3": (986, 5)}), _fw(tmp_path))
    assert "agent user agent-ws3 shares an identity" in out
    out = _run(state, _acc(groups={980: frozenset({"orch-hub", "agent-ws2"})}), _fw(tmp_path))
    assert "FAIL host and agent users are separate from the hub: agent user agent-ws2 is in group orch-hub" in out


def test_p2_style_host_without_agent_user_is_refused(state, tmp_path):
    out = _run(state, _acc(drop=("agent-ws2",)), _fw(tmp_path))
    assert "FAIL hosts use the server custody profile: orch-host-ws2 has no agent user agent-ws2" in out


@pytest.mark.parametrize(
    "ids,needle",
    [
        ('0, "orch-hub", "orch-host-ws2", "agent-ws2"', "not root, orch-hub or an orch-host-* user"),
        ("0, 991, 986, 992", "not root, orch-hub or an orch-host-* user"),
        ('0, "orch-host-ws2"', "does not allow the orch-hub uid"),
        ('0, "orch-hub", "nobody-here"', "unknown user"),
    ],
)
def test_link_firewall_file_failures(state, tmp_path, ids, needle):
    out = _run(state, _acc(), _fw(tmp_path, ids))
    assert "FAIL link firewall allows only root, hub and host users" in out
    assert needle in out, out


def test_link_firewall_missing_or_ruleless_or_wrong_port_fails(state, tmp_path):
    assert "missing or unreadable" in _run(state, _acc(), tmp_path / "nope")
    f = tmp_path / "x.nft"
    f.write_text("table inet orch_hub {}")
    assert "no 'tcp dport 8105" in _run(state, _acc(), f)
    assert "no 'tcp dport 8105" in _run(state, _acc(), _fw(tmp_path, port=9999))
    assert "no 'tcp dport 8105" in _run(state, _acc(), _fw(tmp_path, verdict="accept"))


def test_link_firewall_ignores_comments(state, tmp_path):
    out = _run(
        state,
        _acc(),
        _fw(tmp_path, ids='0, "orch-hub", "orch-host-ws2"', pre="# tcp dport 8105 meta skuid != { 0, 991 } reject\n"),
    )
    assert "PASS link firewall allows only" in out
    f = tmp_path / "c.nft"
    f.write_text("# tcp dport 8105 meta skuid != { 0, 986, 992 } reject\n")
    assert "no 'tcp dport 8105" in _run(state, _acc(), f)


def test_link_firewall_needs_listen(state, tmp_path):
    assert "needs --listen" in _run(state, _acc(), _fw(tmp_path), listen=None)


def test_loaded_table_must_agree_with_the_file(state, tmp_path):
    fw = _fw(tmp_path)
    # an extra uid in the kernel
    extra = _dump(tmp_path / "a.json", [_jrule("out", iface="lo", ids={0, 986, 991, 992})])
    out = _run(state, _acc(), fw, dump=extra)
    assert "FAIL link firewall allows only root, hub and host users: loaded ruleset allows uid(s) [991]" in out
    # a different (narrower) uid set: not a security hole, but drift all the same
    narrow = _dump(tmp_path / "b.json", [_jrule("out", iface="lo", ids={0, 986})])
    assert "the loaded ruleset differs from the firewall file" in _run(state, _acc(), fw, dump=narrow)
    # the old hand-loaded form (ip daddr, no oifname lo)
    old = _dump(tmp_path / "c.json", [_jrule("out", ids={0, 986, 992})])
    assert "the rule must match oifname lo (found none)" in _run(state, _acc(), fw, dump=old)
    # wrong port, and no rule at all
    wrong = _dump(tmp_path / "d.json", [_jrule("out", iface="lo", port=8106, ids={0, 986, 992})])
    assert "loaded ruleset: no 'tcp dport 8105" in _run(state, _acc(), fw, dump=wrong)
    none = _dump(tmp_path / "e.json", [])
    assert "loaded ruleset: no 'tcp dport 8105" in _run(state, _acc(), fw, dump=none)


def test_missing_or_garbage_dump_fails(state, tmp_path):
    fw = _fw(tmp_path)
    assert "loaded-rule dump" in _run(state, _acc(), fw, dump=tmp_path / "nope.json")
    assert "missing: orch-hub-fw.service writes it" in _run(state, _acc(), fw, dump=tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert "is not JSON" in _run(state, _acc(), fw, dump=bad)
    bad.write_text("[]")
    assert "is not nft JSON" in _run(state, _acc(), fw, dump=bad)


def test_file_without_oifname_lo_fails(state, tmp_path):
    f = tmp_path / "hub-fw.nft"
    f.write_text(
        "table inet orch_hub { chain out { tcp dport 8105 meta skuid != { 0, orch-hub, orch-host-ws2 } reject } }"
    )
    out = _run(state, _acc(), f)
    assert "firewall file: the rule must match oifname lo (found none)" in out


# ---- split dev layout (D12b, G9) --------------------------------------------------------------------------------

SURFACE = [
    _jrule("in", iface="wt0", iface_key="iifname", port=8106, verdict="accept"),
    _jrule("in", port=8106, verdict="drop"),
]


def _split_acc():
    users = {
        "orch-hub-split": (900, 900),
        "orch-host-ws2": (992, 984),
        "agent-ws2": (991, 983),
        "orch-relay": (995, 987),
    }
    return Accounts(users, {})


SURFACE_FILE = (
    "table inet orch_hub { chain in { type filter hook input priority 0; policy accept;\n"
    '  tcp dport 8106 iifname "wt0" accept\n  tcp dport 8106 drop\n } }\n'
)


def _split(state, tmp_path, rules=None, *, card_dev=True, fw_text=SURFACE_FILE, **kw):
    out = io.StringIO()
    dump = _dump(tmp_path / "split.json", SURFACE if rules is None else rules)
    fw = tmp_path / "split-fw.nft"
    fw.write_text(fw_text)
    kw.setdefault("fw_file", fw)
    run(
        state,
        link_listen=kw.pop("link_listen", "100.71.244.99:8106"),
        out=out,
        accounts=kw.pop("acc", _split_acc()),
        fw_dump=dump,
        hub_user="orch-hub-split",
        state_uid=900,
        unit_user=lambda: "orch-hub-split",
        dev_shared_machine=kw.pop("dev_shared", True),
        **kw,
    )
    return out.getvalue()


def test_split_hub_dev_exception_warns_and_checks_surface(state, tmp_path):
    out = _split(state, tmp_path)
    assert "PASS network link bind is allowed here" in out
    assert "WARN dev split on a shared machine: this proves the network link, not machine separation" in out
    assert "PASS hub runs as its own OS user" in out  # hub_user is a parameter
    assert "PASS network link port is limited to wt0" in out
    assert "TLS pin" in out  # the pin check runs against the link address (it fails here: nothing listens)


def test_split_hub_without_the_flag_fails_with_host_users(state, tmp_path):
    out = _split(state, tmp_path, dev_shared=False)
    assert "FAIL network link bind is allowed here" in out and "split layout" in out


@pytest.mark.parametrize("addr", ["0.0.0.0:8106", "[::]:8106", "8.8.8.8:8106", "example.com:8106"])
def test_split_hub_bad_bind_addresses_fail(state, tmp_path, addr):
    out = _split(state, tmp_path, link_listen=addr)
    assert "FAIL network link bind is allowed here" in out, out


def test_split_hub_surface_failures(state, tmp_path):
    assert "no 'tcp dport 8106 iifname wt0 accept'" in _split(state, tmp_path, rules=[SURFACE[1]])
    assert "no 'tcp dport 8106 drop'" in _split(state, tmp_path, rules=[SURFACE[0]])
    assert "comes before" in _split(state, tmp_path, rules=[SURFACE[1], SURFACE[0]])
    any_if = _jrule("in", iface="eth0", iface_key="iifname", port=8106, verdict="accept")
    assert "accepted on an interface other than wt0" in _split(state, tmp_path, rules=[*SURFACE, any_if])
    assert "FAIL network link port is limited to wt0" in _split(state, tmp_path, rules=[])


def test_clean_split_machine_has_no_exception_warning(state, tmp_path):
    acc = Accounts({"orch-hub-split": (900, 900)}, {})
    out = io.StringIO()
    run(
        state,
        link_listen="10.1.2.3:8106",
        out=out,
        accounts=acc,
        hub_user="orch-hub-split",
        state_uid=900,
        dev_shared_machine=False,
    )
    assert "PASS network link bind is allowed here" in out.getvalue()
    assert "WARN" not in out.getvalue()
    assert "hub alone" in out.getvalue()


def test_listen_and_link_listen_are_exclusive(state):
    out = io.StringIO()
    run(state, listen="127.0.0.1:1", link_listen="10.1.2.3:8106", out=out, accounts=Accounts({}, {}), state_uid=0)
    assert "FAIL listen addresses" in out.getvalue()


def test_split_surface_file_and_loaded_table_must_agree(state, tmp_path):
    out = _split(state, tmp_path)  # only --link-listen given: the surface check still compares file and dump
    assert "PASS network link port is limited to wt0" in out
    # the file says wt0, the kernel has eth0 (hand-loaded rule)
    eth = [
        _jrule("in", iface="eth0", iface_key="iifname", port=8106, verdict="accept"),
        _jrule("in", port=8106, verdict="drop"),
    ]
    out = _split(state, tmp_path, rules=eth)
    assert "FAIL network link port is limited to wt0: loaded ruleset: no 'tcp dport 8106 iifname wt0 accept'" in out
    # the file drops the accept rule: the dump has it, the file does not
    out = _split(state, tmp_path, fw_text="table inet orch_hub { chain in { tcp dport 8106 drop } }\n")
    assert "firewall file: no 'tcp dport 8106 iifname wt0 accept'" in out
    # same shape, but the loaded table has an extra accept for the port on a second rule order: drift is reported
    swapped = [SURFACE[0], _jrule("in", iface="wt0", iface_key="iifname", port=8106, verdict="accept"), SURFACE[1]]
    out = _split(state, tmp_path, rules=swapped)
    assert "the loaded input rules differ from the firewall file" in out


def test_split_missing_file_fails(state, tmp_path):
    out = _split(state, tmp_path, fw_file=tmp_path / "nope.nft")
    assert "FAIL network link port is limited to wt0: " in out and "missing or unreadable" in out


def test_dump_with_range_set_is_a_fail_not_a_crash(state, tmp_path):
    fw = _fw(tmp_path)
    weird = _dump(tmp_path / "w.json", [_jrule("out", iface="lo", ids={0})])
    data = json.loads(weird.read_text())
    expr = data["nftables"][-1]["rule"]["expr"]
    next(e for e in expr if "match" in e and e["match"]["op"] == "!=")["match"]["right"] = {
        "set": [{"range": [0, 1000]}]
    }
    weird.write_text(json.dumps(data))
    out = _run(state, _acc(), fw, dump=weird)
    assert "FAIL link firewall allows only root, hub and host users: loaded-rule dump has an unsupported shape" in out
    assert "unexpected TypeError" not in out
