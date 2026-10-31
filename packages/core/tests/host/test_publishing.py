"""P5: the Publish add-on on the host (design-publish-addon §2.2, §3): ``publish.prepare`` / ``share`` / ``send`` /
``revoke`` / ``list`` / ``discard`` / ``register`` end to end on a real served workspace, against a fake server that
speaks orch-publish's routes (through a hub: there is no direct path, V3-D36).

The invariants under test (I1, I2, I12 and the signing rules): nothing leaves without a person's signature over the
exact words; an agent only prepares; the link is returned once and kept nowhere; the bytes sent are the staged bytes;
every request is signed with the workspace key for the configured host."""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from orch import crypto
from orch.crypto import suite as S
from orch.host import hubops, hubpolicy, publishing, signcontent
from orch.host.errors import HostError
from orch.host.publishing import PUBLISH_OPS
from orch.hubclient import HubRefused
from orch.publishclient import PublishError, approval, flow
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.human_helpers import Human
from tests.host.test_artifact_get import stored
from tests.host.test_blobs import add, sha, upload
from tests.host.test_click import Click, click  # noqa: F401
from tests.host.test_hub_ops import HUB_ID, connect_args, join_args, keystore  # noqa: F401  (fixture: a full join)
from tests.hub import contract as H
from tests.hub.fake import BareLink, sign_with
from tests.hub.vec import private_key, pub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

SERVER = "http://127.0.0.1:8103"
SERVER_NOW_MS = 1_800_000_000_000
SHARE = "http://127.0.0.1:8104"
ADDON = {
    "name": "publish",
    "version": "0.1.0",
    "package_sha256": "sha256:" + "0" * 64,
    "capabilities": ["network"],
    "fields": {},
    "sections": [],
    "artifact_kinds": [],
}
LINK = re.compile(re.escape(SHARE) + r"/s/([0-9a-f]{32})#t=([A-Za-z0-9_-]{43})")


class FakeServer:
    """orch-publish's routes, enough for the host: it records every request and keeps shares in memory. ``fail`` is a
    list of ``(method, path-regex, status, error)`` rules consumed once each."""

    def __init__(self, wsk_pub: bytes, host: str = "127.0.0.1:8103") -> None:
        self.wsk_pub, self.host = wsk_pub, host
        self.requests: list[dict[str, Any]] = []
        self.shares: dict[str, dict[str, Any]] = {}
        self.registered = False
        self.fail: list[tuple[str, str, int, str]] = []
        self.lose: list[tuple[str, str]] = []  # (method, path-regex): the server does it, the answer never arrives
        self.bad_signatures: list[str] = []

    def _authentic(self, headers, method, netloc, target, body) -> bool:
        """The hub-path signature (contract 11.1): the host's own ``WSK`` signature over the hub-path bytes, and the
        hub's transport signature under ``HK_op``. Nothing else counts: a direct request has no ``Orch-Hub``."""
        h = {k.lower(): v for k, v in headers.items()}
        if "orch-hub" not in h or "orch-hub-signature" not in h or "orch-owner-approval" in h:
            return False
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), method, netloc,
            target, body, h["orch-hub"], None,
        )  # fmt: skip
        return (
            h["orch-hub"] == HUB_ID
            and S.verify(self.wsk_pub, crypto.unb64u(h["orch-signature"], 64), signed)
            and S.verify(pub("hk_op"), crypto.unb64u(h["orch-hub-signature"], 64), H.hub_publish_bytes(HUB_ID, signed))
        )

    def __call__(self, scheme, netloc, method, target, headers, body, timeout):
        got = self._serve(scheme, netloc, method, target, headers, body, timeout)
        for i, (m, rx) in enumerate(self.lose):
            if m == method and re.search(rx, target):
                del self.lose[i]
                raise OSError("answer lost")
        return got

    def _serve(self, scheme, netloc, method, target, headers, body, timeout):
        self.requests.append({"method": method, "target": target, "body": body, "netloc": netloc, "headers": headers})
        if netloc != self.host or not self._authentic(headers, method, netloc, target, body):
            self.bad_signatures.append(f"{method} {target}")
            return 401, b'{"error":"bad_signature"}'
        for i, (m, rx, status, error) in enumerate(self.fail):
            if m == method and re.search(rx, target):
                del self.fail[i]
                if status == 0:
                    raise OSError("connection reset")
                return status, json.dumps({"error": error}).encode()
        parts = target.split("/")[4:]  # /v1/ws/<ws>/...
        if method == "POST" and parts == ["register"]:
            self.registered = True
            return 200, b'{"workspace":"x","result":"registered"}'
        if method == "GET" and parts == ["shares"]:
            rows = [{"id": k, "state": v["state"], "views": v.get("views", 0)} for k, v in self.shares.items()]
            return 200, json.dumps({"shares": rows}).encode()
        if method == "PUT" and len(parts) == 2:
            meta = json.loads(body)
            self.shares[parts[1]] = {"meta": meta, "state": "staged", "files": {}}
            return 200, b'{"result":"created"}'
        if method == "PUT" and len(parts) >= 4 and parts[2] == "files":
            self.shares[parts[1]]["files"]["/".join(parts[3:])] = body
            return 200, b'{"result":"stored"}'
        if method == "POST" and parts[2:] == ["commit"]:
            self.shares[parts[1]]["state"] = "live"
            return 200, b'{"result":"committed"}'
        if method == "DELETE" and len(parts) == 2:
            self.shares[parts[1]]["state"] = "revoked"
            return 200, b'{"result":"revoked"}'
        if method == "POST" and len(parts) == 3 and parts[2] == "extend" and parts[1] in self.shares:
            days = json.loads(body)["days"]
            self.shares[parts[1]]["extended"] = days
            return 200, json.dumps({"id": parts[1], "expires_ms": SERVER_NOW_MS + days * 86_400_000}).encode()
        if method == "POST" and len(parts) == 3 and parts[2] == "password" and parts[1] in self.shares:
            self.shares[parts[1]]["pw"] = json.loads(body)["pw"]
            return 200, json.dumps({"id": parts[1], "has_password": True}).encode()
        return 404, b'{"error":"not_found"}'


class PublishHub(BareLink):
    """The hub link as the host sees it: takes ``join`` and ``card_put`` (marking the fake server registered, or the
    ``error`` set by a test), forwards ``publish_out`` to :class:`FakeServer` adding the hub's signature, as the
    contract says, and answers every message signed by the vector HK_op."""

    def __init__(self, server: FakeServer) -> None:
        self.server, self.sent, self.error = server, [], None

    def ops(self):
        skipped = ("status_put", "profile_put")  # display-only reports, not under test here
        return [m["o"]["op"] for m in self.sent if m["o"]["op"] not in skipped]

    def send(self, message):
        self.sent.append(copy.deepcopy(message))
        m = message["o"]
        body = {}
        if m["op"] == "card_put":
            body = {"error": self.error} if self.error else {}
            if not self.error:
                self.server.registered = True
        elif m["op"] == "publish_out":
            body = {"error": self.error} if self.error else self._forward(m["body"]["request"])
            if "status" not in body and "error" not in body:  # orch-publish could not be reached
                body = {"error": "unavailable"}
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": m["hub_id"], "workspace_id": m["workspace_id"],
            "dir": "h2w", "seq": 1, "ts_ms": m["ts_ms"], "op": m["op"], "re": m["seq"], "body": body,
        }  # fmt: skip
        return sign_with(private_key("hk_op"), "sig_hub_route", o)

    def _forward(self, req):
        h = {k.lower(): v for k, v in req["headers"].items()}
        assert set(h) == {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature"}, "the host's header set (11.1)"
        signed = H.hub_path_signed_bytes(
            bytes.fromhex(h["orch-workspace"]), int(h["orch-ts"]), bytes.fromhex(h["orch-nonce"]), req["method"],
            req["host"], req["target"], bytes.fromhex(req["body_hex"]), HUB_ID, None,
        )  # fmt: skip
        assert S.verify(self.server.wsk_pub, crypto.unb64u(h["orch-signature"], 64), signed), "host signature"
        headers = dict(req["headers"]) | {"Orch-Hub": HUB_ID}
        headers["Orch-Hub-Signature"] = crypto.b64u(S.sign(private_key("hk_op"), H.hub_publish_bytes(HUB_ID, signed)))
        try:
            status, data = self.server(
                "https", req["host"], req["method"], req["target"], headers, bytes.fromhex(req["body_hex"]), 1.0
            )
        except OSError:
            return {}  # the hub's forward failed: it answers `unavailable` (send() turns {} into that)
        return {"status": status, "body_hex": data.hex()}


class World:
    """A served workspace that has joined a hub (``join=False``: a workspace alone). ``server`` is orch-publish behind
    the hub; ``hub`` is the link to it. The module seams are set through ``monkeypatch``, so they are back whenever
    the test ends, closed or not (a raw swap that one test never undid made every later hub test in that xdist worker
    talk to this stale hub and fail ``bad_signature``)."""

    def __init__(self, served, human: Human, monkeypatch, *, join: bool = True) -> None:
        self.served, self.human, self.ws = served, human, served.lab.ws
        self.c = human.c
        genesis = self._genesis()
        self.server = FakeServer(crypto.unb64u(genesis["wsk_pub"]))
        self.hub = PublishHub(self.server)
        card = {"delegation": {"o": {"wsk_pub": genesis["wsk_pub"]}}, "o": {}, "sig": "", "sealed": ""}
        monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: self.hub)
        monkeypatch.setattr(hubops, "CARD_SOURCE", lambda service: card)
        monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
        human_helpers.sync_dev_store(served)  # #1198 C9: the store is dev when workspace_dev says so
        monkeypatch.setattr(hubpolicy, "stored", lambda service: None)  # the Publish tests are not about the hub policy
        if join:
            self.join()

    def join(self, mode: str = "selective") -> None:
        self.human.run("hub.join", join_args(mode=mode))
        ok(self.served.client().req("hub.connect", connect_args(), idem=True))

    def _genesis(self) -> dict[str, Any]:
        store = self.ws.other()
        try:
            return store.genesis_event()
        finally:
            store.close()

    def append_workspace(self, typ: str, **fields: Any) -> None:
        def run(store: Any) -> None:
            self.ws.tick()
            store.append(self.ws.person_event(self.ws.owner, "workspace", typ, **fields), log="workspace")

        self.served.append(run)

    def addon(self, on: bool = True) -> None:
        self.append_workspace("addon.granted", **ADDON)

    def configure(self, server: str = SERVER, share: str = SHARE) -> None:
        self.append_workspace("settings.changed", set={"publish": {"server": server, "share": share}})

    _n = 0

    def ticket_with(self, **files: bytes) -> tuple[str, str]:
        World._n += 1  # a new title each time: the same `new` twice in one session is one ticket
        uid, key = self.served.ticket(f"A ticket with files {World._n}")
        c = self.served.client()
        for name, data in files.items():
            blob = upload(c, data, grant=self.served.grant)
            ok(add(self.served, c, key, {"path": {"blob": blob}}, name=name))
        return uid, key

    def req(self, op: str, args: dict[str, Any], *, agent: bool = False, **kw: Any) -> dict[str, Any]:
        if agent:
            kw.update(grant=self.served.grant, session=SESSION)
        return self.c.req(op, args, **kw)

    def prepare(self, key: str, *names: str, agent: bool = False, **args: Any) -> dict[str, Any]:
        return ok(self.req("publish.prepare", {"ref": key, "artifacts": list(names), **args}, agent=agent))["data"]

    def approve(self, staged_id: str) -> dict[str, Any]:
        prepared, result, _ = self.human.run("publish.share", {"staged_id": staged_id})
        return prepared

    def send(self, staged_id: str, **kw: Any) -> dict[str, Any]:
        return self.req("publish.send", {"staged_id": staged_id}, **kw)

    def stage_dir(self, staged_id: str) -> Path:
        return Path(self.served.lab.state) / "hosts" / self.served.wid / "publish" / "stage" / staged_id

    def base(self) -> Path:
        return Path(self.served.lab.state) / "hosts" / self.served.wid / "publish"

    def everything_on_disk(self) -> bytes:
        out = b""
        for root in (self.served.lab.state, self.served.lab.ws.root):
            for p in Path(root).rglob("*"):
                if p.is_file() and not p.is_symlink() and p.stat().st_size < 5_000_000:
                    out += p.read_bytes()
        return out


@pytest.fixture
def w(served, human, monkeypatch):
    world = World(served, human, monkeypatch)
    world.addon()
    world.configure()
    return world


@pytest.fixture
def bare(served, human, monkeypatch):
    return World(served, human, monkeypatch)


REPORT = b"# Report\nthe numbers are fine\n"
DATA = os.urandom(3000)


# -- the approval words ----------------------------------------------------------------------------------------------


def files(*names):
    return [{"name": n, "size": 10 + i, "sha256": hashlib.sha256(n.encode()).hexdigest()} for i, n in enumerate(names)]


def words(**kw):
    base = dict(
        staged_id="ps_" + "a" * 32, ticket_key="TST-0001", origin="person", kind="secret", days=7, views=0,
        files=files("report.md"), server=SERVER, share=SHARE, manifest="sha256:" + "b" * 64,
    )  # fmt: skip
    return approval.approval_text(**{**base, **kw})


def test_the_words_name_every_file_the_server_and_the_link_kind():
    text = words(files=files("report.md", "data.csv"), kind="once", views=3, origin="agent", days=1)
    lines = text.split("\n")
    assert lines[0] == approval.MARK
    assert "Ticket: TST-0001" in lines and "Prepared by: an agent" in lines
    assert any(line.startswith("Link: a show-once link: it opens at most 3 times") for line in lines)
    assert "Works for: 1 day from the moment the link is made" in lines
    assert f"The files are sent to: {SERVER}" in lines and f"The link opens at: {SHARE}" in lines
    assert sum(line.startswith("- ") for line in lines) == 2 and "- data.csv  (11 bytes, sha256 " in text
    assert "Files (2, 21 bytes):" in lines and text.endswith("Manifest: sha256:" + "b" * 64)


@pytest.mark.parametrize(
    "bad",
    [
        {"files": files("a\nb.md")}, {"files": files("../x")}, {"files": files(".hidden")}, {"files": files("é.md")},
        {"files": files("a" * 101)}, {"files": []}, {"kind": "once", "views": 2}, {"kind": "secret", "views": 3},
        {"days": 2}, {"server": SERVER + "\nLink: harmless"}, {"share": "x y"}, {"ticket_key": "TST-1\nmore"},
        {"origin": "robot"}, {"staged_id": "ps_x"}, {"manifest": "sha256:zz"},
    ],
)  # fmt: skip
def test_the_words_refuse_anything_that_could_fake_a_line_or_a_value(bad):
    with pytest.raises(ValueError):
        words(**bad)


def test_the_manifest_hash_covers_the_server_the_kind_the_days_and_every_file():
    base = ("ws", "uid", "secret", 7, 0, "person", files("a.md"), SERVER, SHARE)
    one = approval.manifest_hash(*base)
    assert one == approval.manifest_hash(*base) and one.startswith("sha256:")
    for i, other in enumerate(["ws2", "uid2", "once", 3, 1, "agent", files("b.md"), SERVER + "x", SHARE + "x"]):
        changed = list(base)
        changed[i] = other
        assert approval.manifest_hash(*changed) != one, i


def test_the_token_hash_is_the_servers_contract():
    token = "A" * 43
    assert flow.token_hash(token) == hashlib.sha256(token.encode("ascii")).hexdigest()
    meta = json.loads(
        flow.meta_bytes(
            ticket="TST-0001", kind="once", expires_ms=5, view_limit=1, token_hash_hex="c" * 64, files=files("a.md")
        )  # fmt: skip
    )
    assert set(meta) == {"ticket", "kind", "expires_ms", "view_limit", "token_hash", "files"}
    assert meta["files"] == [{"path": "a.md", "size": 10, "sha256": hashlib.sha256(b"a.md").hexdigest()}]


# -- the gate and the setting ----------------------------------------------------------------------------------------


def test_every_op_is_off_until_the_addon_is_granted(bare):
    uid, key = bare.ticket_with(**{"report.md": REPORT})
    bare.configure()
    for op, args in [("publish.prepare", {"ref": key, "artifacts": ["report.md"]}), ("publish.list", {})]:
        res = bare.req(op, args)
        assert err(res) == "runner.disabled" and res["error"]["detail"] == {"missing": "addon"}
    assert err(bare.req("publish.send", {"staged_id": "ps_" + "0" * 32})) == "runner.disabled"
    assert err(bare.req("publish.register", {})) == "runner.disabled"


def test_without_a_server_setting_prepare_says_so(bare):
    bare.addon()
    uid, key = bare.ticket_with(**{"report.md": REPORT})
    res = bare.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})
    assert err(res) == "publish.not_configured"
    assert not (bare.base() / "stage").exists() or not list((bare.base() / "stage").iterdir())


def test_the_server_setting_is_signed_by_the_owner_and_shown_in_words(bare, human):
    bare.addon()
    prepared = human.prepare("settings.set", {"set": {"publish": {"server": SERVER, "share": SHARE}}})
    content = signcontent.build(prepared, verified=True)
    rows = {r["field"]: r["after"] for r in content["fields_changed"]}
    assert rows == {"Publish: files are sent to": SERVER, "Publish: links open at": SHARE}
    assert any("Only a person can change it" in n for n in content["notes"])
    assert any("Files are sent to: this computer. Links open at: this computer." in n for n in content["notes"])
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    listing = ok(bare.req("publish.list", {}))["data"]
    assert listing["configured"] is True and listing["server"] == SERVER and listing["share"] == SHARE
    refused = human.prepare_res("settings.set", {"set": {"publish": None}})  # AE4: not while Publish is on
    assert (
        not refused["ok"]
        and refused["error"]["code"] == "invalid.input"
        and "turn it off" in refused["error"]["message"]
    )
    off = human.prepare("addon.disable", {"name": "publish"})
    ok(human.submit_res(off["request"]["id"], human.sign(off).events))
    cleared = human.prepare("settings.set", {"set": {"publish": None}})
    assert signcontent.build(cleared, verified=True)["fields_changed"][0]["after"].startswith("(removed")


@pytest.mark.parametrize(
    "value",
    [
        {"server": "http://example.com", "share": "https://apps.example.com"},  # http only for this computer
        {"server": "https://Pub.Example.com", "share": "https://apps.example.com"},
        {"server": "https://pub.example.com/v1", "share": "https://apps.example.com"},
        {"server": "https://pub.example.com"},
        {"server": "https://pub.example.com", "share": "https://apps.example.com", "extra": "x"},
        {"server": "ftp://pub.example.com", "share": "https://apps.example.com"},
    ],
)
def test_a_bad_server_setting_is_refused_before_anything_is_signed(bare, human, value):
    res = human.prepare_res("settings.set", {"set": {"publish": value}})
    assert not res["ok"]


def test_the_builtin_manifest_is_installed_and_granted_like_the_other_addons(bare, human):
    """``addon.grant publish`` (signed) puts the shipped manifest in place and turns the add-on on: no clone needed."""
    from orch.addons import builtin

    assert "publish" in builtin.NAMES and json.loads(builtin.manifest_bytes("publish"))["capabilities"] == ["network"]
    bare.configure()
    assert err(bare.req("publish.list", {})) == "runner.disabled"
    prepared = human.prepare("addon.grant", {"name": "publish"})
    content = signcontent.build(prepared, verified=True)
    assert content["headline"] == "Turn on Publish: it says it will use the internet"
    assert any(r["field"] == "it says it will" and "internet" in r["after"] for r in content["fields_changed"])
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    assert (Path(bare.ws.store_dir) / "addons" / "publish" / "orch-addon.json").read_bytes() == builtin.manifest_bytes(
        "publish"
    )
    assert ok(bare.req("publish.list", {}))["data"]["configured"] is True  # the gate is open
    off = human.prepare("addon.disable", {"name": "publish"})
    ok(human.submit_res(off["request"]["id"], human.sign(off).events))
    assert err(bare.req("publish.list", {})) == "runner.disabled"


# -- prepare ---------------------------------------------------------------------------------------------------------


def test_prepare_copies_the_artifacts_aside_and_nothing_leaves(w):
    uid, key = w.ticket_with(**{"report.md": REPORT, "data.csv": DATA})
    got = w.prepare(key, "report.md", "data.csv", days=3)
    assert got["staged_id"].startswith("ps_") and got["origin"] == "person" and got["kind"] == "secret"
    assert got["needs"] == "a person's signature" and got["days"] == 3
    assert [(f["name"], f["size"], f["sha256"]) for f in got["files"]] == [
        ("report.md", len(REPORT), hashlib.sha256(REPORT).hexdigest()),
        ("data.csv", len(DATA), hashlib.sha256(DATA).hexdigest()),
    ]
    stage = w.stage_dir(got["staged_id"])
    assert (stage / "files" / "report.md").read_bytes() == REPORT and (
        stage / "files" / "data.csv"
    ).read_bytes() == DATA
    assert oct(stage.stat().st_mode & 0o777) == "0o700" and oct((stage / "meta.json").stat().st_mode & 0o777) == "0o600"
    assert oct((stage / "files" / "report.md").stat().st_mode & 0o777) == "0o600"
    assert w.server.requests == []  # nothing left the machine
    assert "token" not in json.dumps(got).lower() and not (stage / "token").exists()
    assert got["words"].startswith(approval.MARK) and "report.md" in got["words"] and SERVER in got["words"]


def test_prepare_refuses_what_the_ticket_does_not_have_or_the_server_would_not_take(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["nope.md"]})) == "not_found"
    assert err(w.req("publish.prepare", {"ref": "TST-9999", "artifacts": ["report.md"]})) == "not_found"
    for bad in ("../ticket.json", "/etc/passwd", ".hidden", "a b/c"):
        assert err(w.req("publish.prepare", {"ref": key, "artifacts": [bad]})) in ("not_found", "invalid.input"), bad
    for args in (
        {"artifacts": []}, {"artifacts": ["report.md", "report.md"]}, {"artifacts": ["report.md"], "days": 2},
        {"artifacts": ["report.md"], "kind": "once", "views": 2}, {"artifacts": ["report.md"], "views": 1},
        {"artifacts": ["report.md"], "kind": "secret", "views": 3}, {"artifacts": ["report.md"], "extra": 1},
        {"artifacts": ["x" * 129]},
    ):  # fmt: skip
        assert err(w.req("publish.prepare", {"ref": key, **args})) == "invalid.input", args
    once = w.prepare(key, "report.md", kind="once")
    assert once["kind"] == "once" and once["views"] == 1
    assert w.prepare(key, "report.md", kind="once", views=10)["views"] == 10


def test_prepare_refuses_a_file_that_changed_behind_the_log(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    path = Path(w.served.lab.ws.store_dir) / "tickets" / uid / "artifacts" / "report.md"
    path.write_bytes(b"tampered")
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})) == "not_found"
    assert not (w.base() / "stage").exists() or not list((w.base() / "stage").iterdir())


def test_prepare_refuses_a_file_over_the_servers_limit(w, monkeypatch):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    monkeypatch.setitem(approval.LIMITS, "file_bytes", 10)
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})) == "quota.host"
    monkeypatch.setitem(approval.LIMITS, "file_bytes", 1000)
    monkeypatch.setitem(approval.LIMITS, "share_bytes", 10)
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})) == "quota.host"


def test_at_most_twenty_staged_copies_wait(w, monkeypatch):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    monkeypatch.setattr(publishing, "MAX_STAGED", 2)
    monkeypatch.setattr(publishing, "PER_MINUTE", 100)
    w.prepare(key, "report.md")
    w.prepare(key, "report.md")
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})) == "quota.host"


def test_prepare_is_rate_limited_per_person(w, monkeypatch):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    for _ in range(publishing.PER_MINUTE):
        w.prepare(key, "report.md")
    res = w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]})
    assert err(res) == "rate.limited" and res["error"]["retry_after_ms"] > 0


# -- an agent ---------------------------------------------------------------------------------------------------------


def test_an_agent_prepares_only_on_the_ticket_it_claims_and_is_tagged_agent(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    other_uid, other = w.ticket_with(**{"o.md": b"x"})
    agent = w.served.client()
    # not claimed yet: refused
    res = agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed"
    ok(agent.req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    got = ok(agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant,
                       session=SESSION))["data"]  # fmt: skip
    assert got["origin"] == "agent" and "Prepared by: an agent" in got["words"]
    res = agent.req("publish.prepare", {"ref": other, "artifacts": ["o.md"]}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed"
    # it cannot sign, send, revoke, register, and no one-click path exists for it
    for op, args in [
        ("publish.send", {"staged_id": got["staged_id"]}),
        ("publish.revoke", {"share_id": "0" * 32}),
        ("publish.register", {}),
    ]:
        assert err(agent.req(op, args, grant=w.served.grant, session=SESSION)) == "op.not_allowed", op
    assert err(agent.req("human.prepare", {"op": "publish.share", "args": {"staged_id": got["staged_id"]},
                                           "handoff": True}, grant=w.served.grant, session=SESSION)) in {
        "human_only", "op.not_allowed"}  # fmt: skip
    assert w.server.requests == []


def test_a_person_without_a_grant_is_not_an_agent_and_no_person_reaches_nothing(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    nobody = w.served.client()
    res = nobody.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant="gr_not-a-real-grant")
    assert err(res) in {"grant.expired", "grant.required", "op.not_allowed"}


# -- the signature and the send --------------------------------------------------------------------------------------


def test_send_without_a_signature_is_refused_and_nothing_is_sent(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    assert err(w.send(staged)) == "publish.not_approved"
    assert w.server.requests == [] and (w.stage_dir(staged) / "meta.json").exists()


def test_the_review_shows_the_exact_files_server_and_link_kind(w):
    uid, key = w.ticket_with(**{"report.md": REPORT, "data.csv": DATA})
    got = w.prepare(key, "report.md", "data.csv", kind="once", views=3, days=1)
    prepared = w.human.prepare("publish.share", {"staged_id": got["staged_id"]})
    (event,) = prepared["events"]
    assert event["type"] == "log.added" and event["text"] == got["words"]
    assert prepared["log"] == uid and prepared["op"] == "publish.share"
    content = signcontent.build(prepared, verified=True)
    assert content["headline"].startswith(f"Share files of {key}") and content["headline"].endswith(
        "outside this computer"
    )
    (section,) = content["sections"]
    for needle in ("report.md", "data.csv", SERVER, SHARE, "show-once link", "at most 3 times", "1 day", key):
        assert needle in section["text"], needle
    assert hashlib.sha256(REPORT).hexdigest()[:16] in section["text"]
    assert any("only after you sign" in n for n in content["notes"])
    w.human.check(prepared)  # the independent signer re-derives and accepts the review


def test_a_share_is_signed_with_a_plain_click_and_the_view_shows_everything(w, click):  # noqa: F811
    """#1198 C2: ``publish.share`` is a click op; its View carries the signed words: files, link kind, both servers."""
    uid, key = w.ticket_with(**{"report.md": REPORT})
    got = w.prepare(key, "report.md", kind="once", views=3, days=1)
    p = click.prepare("publish.share", {"staged_id": got["staged_id"]})
    shown = click.view(p["request_id"])
    v = shown["view"]
    assert v["confirm"] == {"kind": "click"}
    text = "\n".join(v["lines"])
    for needle in ("report.md", SERVER, SHARE, "show-once link", key):
        assert needle in text, needle
    click.mono.t += 2
    click.sign(p["request_id"], shown["view_digest"])
    assert click.signatures == 1


def test_the_signer_refuses_a_share_approval_that_is_not_one_note_on_a_ticket(w):
    from orch.host import signer

    uid, key = w.ticket_with(**{"report.md": REPORT})
    got = w.prepare(key, "report.md")
    prepared = w.human.prepare("publish.share", {"staged_id": got["staged_id"]})
    bad = json.loads(json.dumps(prepared))
    bad["events"][0]["text"] = "just a note"
    with pytest.raises(signer.SignerRefused):
        w.human.check(bad)
    on_workspace = json.loads(json.dumps(prepared))
    on_workspace["log"] = "workspace"
    with pytest.raises(signer.SignerRefused):
        w.human.check(on_workspace)


def test_prepare_of_the_share_names_only_a_staged_copy_and_builds_the_words_itself(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    res = w.human.prepare_res("publish.share", {"staged_id": "ps_" + "1" * 32})
    assert err(res) == "not_found"
    res = w.human.prepare_res("publish.share", {"staged_id": "ps_" + "1" * 32, "text": "anything"})
    assert err(res) == "invalid.input"
    res = w.human.prepare_res("publish.share", {"staged_id": "../../x"})
    assert err(res) == "invalid.input"


def test_the_whole_flow_prepare_sign_send_and_the_link_is_shown_once(w):
    uid, key = w.ticket_with(**{"report.md": REPORT, "data.csv": DATA})
    staged = w.prepare(key, "report.md", "data.csv", days=3)["staged_id"]
    w.approve(staged)
    res = w.send(staged)
    data = ok(res)["data"]
    m = LINK.fullmatch(data["link"])
    assert m, data["link"]
    share_id, token = m.groups()
    assert data["share_id"] == share_id and data["shown_once"] is True and data["kind"] == "secret"
    # the server got only the hash of the token, the manifest, the exact staged bytes, and the commit
    srv = w.server
    assert srv.bad_signatures == [] and srv.registered
    kinds = [(r["method"], r["target"].split("/", 4)[4]) for r in srv.requests]
    assert "card_put" in w.hub.ops()  # registered as the card through the hub, never a direct POST
    assert kinds == [
        ("PUT", f"shares/{share_id}"),
        ("PUT", f"shares/{share_id}/files/report.md"),
        ("PUT", f"shares/{share_id}/files/data.csv"),
        ("POST", f"shares/{share_id}/commit"),
    ]
    share = srv.shares[share_id]
    assert share["state"] == "live" and share["files"] == {"report.md": REPORT, "data.csv": DATA}
    assert share["meta"]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert share["meta"]["ticket"] == key and share["meta"]["kind"] == "secret" and share["meta"]["view_limit"] == 0
    horizon = share["meta"]["expires_ms"] / 1000 - w.served.service.clock()
    assert 3 * 86400 - 5 <= horizon <= 3 * 86400 + 5
    assert all(token not in r["target"] and token.encode() not in r["body"] for r in srv.requests)
    # shown once: the staged copy and the token are gone, a second send finds nothing
    assert not w.stage_dir(staged).exists()
    assert err(w.send(staged)) == "not_found"
    blob = w.everything_on_disk()
    assert token.encode() not in blob and data["link"].encode() not in blob
    assert base64.b64encode(token.encode()) not in blob
    # the record the host keeps names the share, never the link
    rows = ok(w.req("publish.list", {}))["data"]["shares"]
    assert rows == [{
        "id": share_id, "ticket": key, "kind": "secret", "state": "live", "expires_at": data["expires_at"],
        "view_limit": 0, "views": 0, "files": ["report.md", "data.csv"], "created_at": rows[0]["created_at"],
        "server": SERVER, "has_password": False,
    }]  # fmt: skip
    assert token not in json.dumps(rows)


def test_a_show_once_link_carries_its_view_limit(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md", kind="once", views=3)["staged_id"]
    w.approve(staged)
    data = ok(w.send(staged))["data"]
    meta = w.server.shares[data["share_id"]]["meta"]
    assert meta["kind"] == "once" and meta["view_limit"] == 3 and data["view_limit"] == 3


def test_the_signature_must_match_this_staged_copy_exactly(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    a = w.prepare(key, "report.md")["staged_id"]
    b = w.prepare(key, "report.md")["staged_id"]
    w.approve(a)
    assert err(w.send(b)) == "publish.not_approved"  # the signature named copy a
    assert w.server.requests == []
    ok(w.send(a))


def test_changing_the_server_after_the_signature_voids_it(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.configure(server="http://127.0.0.1:9999", share="http://127.0.0.1:9998")
    assert err(w.send(staged)) == "publish.not_approved"
    assert w.server.requests == []


def test_a_staged_file_changed_after_the_signature_is_not_sent(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    path = w.stage_dir(staged) / "files" / "report.md"
    path.write_bytes(b"swapped" + REPORT[7:])  # same size, other bytes
    assert err(w.send(staged)) == "publish.changed"
    path.unlink()
    assert err(w.send(staged)) == "publish.changed"
    os.symlink("/etc/hosts", path)
    assert err(w.send(staged)) == "publish.changed"
    assert w.server.requests == []


def test_a_note_an_agent_wrote_with_the_same_words_is_no_signature(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")
    agent = w.served.client()
    ok(agent.req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    # the ordinary note op refuses the reserved first line outright (F1); the send finds no signature either way
    res = agent.req("log", {"text": staged["words"], "ref": key}, grant=w.served.grant, session=SESSION, idem=True)
    assert err(res) == "invalid.input"
    assert err(w.send(staged["staged_id"])) == "publish.not_approved"
    assert w.server.requests == []


def test_a_signature_is_good_for_an_hour(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.served.lab.ws.clock[0] += publishing.APPROVAL_VALID + 60
    assert err(w.send(staged)) == "publish.not_approved"


def test_an_agent_cannot_send_even_a_signed_copy_and_gets_no_link(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    agent = w.served.client()
    res = agent.req("publish.send", {"staged_id": staged}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed" and w.server.requests == []
    assert (w.stage_dir(staged) / "meta.json").exists()


# -- failures and retries --------------------------------------------------------------------------------------------


def test_a_lost_answer_is_retried_with_the_same_manifest_and_one_token(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/files/report\.md$", 0, ""))  # the hub's forward is lost: the hub says unavailable
    res = w.send(staged)
    assert err(res) == "publish.unreachable" and res["error"]["retryable"] is True
    assert (w.stage_dir(staged) / "token").exists()  # the token waits in the stage until the server committed
    first_meta = [r["body"] for r in w.server.requests if r["method"] == "PUT" and r["target"].count("/") == 5][0]
    data = ok(w.send(staged))["data"]  # the person runs it again
    metas = [r["body"] for r in w.server.requests if r["method"] == "PUT" and r["target"].count("/") == 5]
    assert set(metas) == {first_meta}  # the same manifest bytes every time: the server answers "unchanged"
    token = LINK.fullmatch(data["link"]).group(2)
    assert json.loads(first_meta)["token_hash"] == hashlib.sha256(token.encode()).hexdigest()


def test_an_unknown_workspace_says_what_the_admin_must_do_and_keeps_the_copy(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.hub.error = "unknown_workspace"  # publish refused the card at the hub
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "unknown_workspace"}
    info = ok(w.req("publish.list", {}))["data"]
    assert re.fullmatch(r"[0-9a-f]{32}", info["workspace"]) and re.fullmatch(r"[0-9a-f]{32}", info["owner"])
    assert (w.stage_dir(staged) / "meta.json").exists()
    # P02: the refusal leaves no "Sending" row in the list and the record is marked failed
    assert ok(w.req("publish.list", {}))["data"]["shares"] == []
    w.hub.error = None
    ok(w.send(staged))  # the admin allowed it; the same copy goes
    assert len(ok(w.req("publish.list", {}))["data"]["shares"]) == 1


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [(403, "workspace_blocked", "blocked"), (403, "owner_not_allowed", "owner_not_allowed"),
     (413, "quota_exceeded", "limit"), (429, "rate_limited", "limit"), (400, "bad_path", "rejected"),
     (409, "conflict", "conflict")],
)  # fmt: skip
def test_the_servers_refusals_become_a_closed_reason(w, status, error, reason):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/shares/[0-9a-f]{32}$", status, error))
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": reason}
    assert set(res["error"]["detail"]) == {"reason"}  # the server's own words are not passed on as detail


def test_a_wrong_clock_is_said_in_words(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/shares/[0-9a-f]{32}$", 401, "stale_timestamp"))
    w.server.fail.append(("PUT", r"/shares/[0-9a-f]{32}$", 401, "stale_timestamp"))
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "clock"}
    assert "clock" in res["error"]["message"]


def test_register_sends_the_workspace_card_and_owner_key_to_the_hub_and_nothing_direct(w):
    res = ok(w.req("publish.register", {}))["data"]
    assert res["registered"] is True and res["server"] == SERVER
    assert w.server.requests == []  # V3-D36: nothing goes to orch-publish from the workspace
    (msg,) = [m["o"] for m in w.hub.sent if m["o"]["op"] == "card_put"]
    genesis = w._genesis()
    assert set(msg["body"]) == {"card", "pk_pub"}  # two keys, nothing else
    assert msg["body"]["pk_pub"] == genesis["owner"]["pk_pub"]
    assert msg["body"]["card"] == hubops.CARD_SOURCE(None)
    assert not (w.base() / "card").exists()
    assert ok(w.req("publish.register", {}))["data"]["registered"] is True
    puts = [m["o"]["body"] for m in w.hub.sent if m["o"]["op"] == "card_put"]
    assert len(puts) == 2 and puts[0] == puts[1]  # identical bytes again: publish answers unchanged


def test_a_server_holding_another_delegation_is_a_conflict_in_words(w):
    w.hub.error = "delegation_changed"
    res = w.req("publish.register", {})
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "conflict"}
    assert "unbind" in res["error"]["message"]


def test_every_request_is_signed_for_the_configured_host_with_the_workspace_key(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    ok(w.send(staged))
    assert w.server.bad_signatures == []
    assert {r["netloc"] for r in w.server.requests} == {"127.0.0.1:8103"}
    assert all(r["headers"]["Orch-Workspace"] == w.served.wid for r in w.server.requests)
    nonces = [r["headers"]["Orch-Nonce"] for r in w.server.requests]
    assert len(set(nonces)) == len(nonces)


# -- revoke, list, discard ---------------------------------------------------------------------------------------------


def shared(w, *names):
    uid, key = w.ticket_with(**{n: REPORT for n in names})
    staged = w.prepare(key, *names)["staged_id"]
    w.approve(staged)
    return key, ok(w.send(staged))["data"]


def test_a_person_revokes_and_the_record_says_so(w):
    key, data = shared(w, "report.md")
    res = ok(w.req("publish.revoke", {"share_id": data["share_id"]}))["data"]
    assert res == {"share_id": data["share_id"], "state": "revoked"}
    assert w.server.shares[data["share_id"]]["state"] == "revoked"
    (row,) = ok(w.req("publish.list", {}))["data"]["shares"]
    assert row["state"] == "revoked"
    assert err(w.req("publish.revoke", {"share_id": "f" * 32})) == "not_found"
    assert err(w.req("publish.revoke", {"share_id": "nope"})) == "invalid.input"
    ok(w.req("publish.revoke", {"share_id": data["share_id"]}))  # again: still revoked, no error


def test_an_agent_cannot_revoke(w):
    key, data = shared(w, "report.md")
    agent = w.served.client()
    res = agent.req("publish.revoke", {"share_id": data["share_id"]}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed" and w.server.shares[data["share_id"]]["state"] == "live"


# -- extend, password (M4 S2, #1453; Opus conditions 8 and 14) -------------------------------------------------------


def test_a_person_extends_a_link_and_the_record_follows_the_server(w):
    key, data = shared(w, "report.md")
    res = ok(w.req("publish.extend", {"share_id": data["share_id"], "days": 30}))["data"]
    assert res["share_id"] == data["share_id"]
    assert res["expires_at"] == publishing._iso((SERVER_NOW_MS + 30 * 86_400_000) / 1000)
    assert w.server.shares[data["share_id"]]["extended"] == 30
    (row,) = ok(w.req("publish.list", {}))["data"]["shares"]
    assert row["expires_at"] == res["expires_at"] and row["state"] == "live"
    assert err(w.req("publish.extend", {"share_id": "f" * 32, "days": 3})) == "not_found"


def test_extend_is_bounded_to_1_to_30_days_before_anything_is_sent(w):
    key, data = shared(w, "report.md")
    sent = len(w.server.requests)
    for days in (0, 31, -1, 365, "3", True, None):  # (a float cannot even be framed)
        assert err(w.req("publish.extend", {"share_id": data["share_id"], "days": days})) == "invalid.input", days
    for bad in ({"share_id": data["share_id"]}, {"days": 3}, {"share_id": data["share_id"], "days": 3, "x": 1}):
        assert err(w.req("publish.extend", bad)) == "invalid.input"
    assert len(w.server.requests) == sent


def test_an_agent_cannot_extend_or_set_a_password(w):
    key, data = shared(w, "report.md")
    sent = len(w.server.requests)
    for op, args in (
        ("publish.extend", {"share_id": data["share_id"], "days": 3}),
        ("publish.password", {"share_id": data["share_id"]}),
    ):
        assert err(w.req(op, args, agent=True)) == "op.not_allowed", op
    assert len(w.server.requests) == sent and "extended" not in w.server.shares[data["share_id"]]


def test_extending_needs_what_sharing_needs(w, monkeypatch):
    key, data = shared(w, "report.md")

    def denied(call, view):
        raise publishing.HostError("role.denied", "sharing is for the ticket owner, owners and maintainers")

    monkeypatch.setattr(publishing, "_may_share", denied)
    sent = len(w.server.requests)
    assert err(w.req("publish.extend", {"share_id": data["share_id"], "days": 3})) == "role.denied"
    assert len(w.server.requests) == sent
    assert err(w.req("publish.password", {"share_id": data["share_id"]})) == "role.denied"  # seeing is not enough
    assert len(w.server.requests) == sent


def test_a_dead_link_is_not_extended(w):
    key, data = shared(w, "report.md")
    ok(w.req("publish.revoke", {"share_id": data["share_id"]}))
    assert err(w.req("publish.extend", {"share_id": data["share_id"], "days": 3})) == "publish.changed"
    assert err(w.req("publish.password", {"share_id": data["share_id"]})) == "publish.changed"


def test_a_password_is_drawn_by_the_host_sent_as_a_c5_record_and_shown_once(w):
    import hashlib

    key, data = shared(w, "report.md")
    res = ok(w.req("publish.password", {"share_id": data["share_id"]}))["data"]
    pw = res["password"]
    assert res["shown_once"] is True and re.fullmatch(r"([a-z]{4}-){4}[a-z]{4}", pw)
    rec = w.server.shares[data["share_id"]]["pw"]
    assert set(rec) == {"alg", "n", "r", "p", "salt", "dk"}
    assert (rec["alg"], rec["n"], rec["r"], rec["p"]) == ("scrypt", 32768, 8, 1)
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    salt, dk = base64.urlsafe_b64decode(pad(rec["salt"])), base64.urlsafe_b64decode(pad(rec["dk"]))
    assert (len(salt), len(dk)) == (16, 32)
    assert hashlib.scrypt(pw.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=128 * 32768 * 8 * 2) == dk
    # the server never saw the plain password, and nothing on disk or in the audit keeps it
    assert all(pw.encode() not in r["body"] for r in w.server.requests)
    assert pw.encode() not in w.everything_on_disk()
    (row,) = ok(w.req("publish.list", {}))["data"]["shares"]
    assert row["has_password"] is True and "password" not in row
    # a second call draws a new password and a new salt
    again = ok(w.req("publish.password", {"share_id": data["share_id"]}))["data"]["password"]
    assert again != pw and w.server.shares[data["share_id"]]["pw"]["salt"] != rec["salt"]


def test_a_password_request_takes_no_password_no_record_and_no_removal(w):
    key, data = shared(w, "report.md")
    sid = data["share_id"]
    for extra in ({"password": "x"}, {"record": {}}, {"pw": None}, {"remove": True}, {"password": None}):
        assert err(w.req("publish.password", {"share_id": sid, **extra})) == "invalid.input", extra
    assert "pw" not in w.server.shares[sid]
    (row,) = ok(w.req("publish.list", {}))["data"]["shares"]
    assert row["has_password"] is False


def test_list_shows_staged_copies_and_shares_but_never_a_link_or_token(w):
    key, data = shared(w, "report.md")
    uid, key2 = w.ticket_with(**{"b.md": b"b"})
    waiting = w.prepare(key2, "b.md")["staged_id"]
    out = ok(w.req("publish.list", {}))["data"]
    assert [s["staged_id"] for s in out["staged"]] == [waiting] and out["staged"][0]["origin"] == "person"
    assert [s["id"] for s in out["shares"]] == [data["share_id"]]
    text = json.dumps(out)
    token = LINK.fullmatch(data["link"]).group(2)
    assert token not in text and "#t=" not in text and "token" not in text.lower()
    assert [s["id"] for s in ok(w.req("publish.list", {"ref": key}))["data"]["shares"]] == [data["share_id"]]
    assert ok(w.req("publish.list", {"ref": key2}))["data"]["shares"] == []


def test_list_live_adds_the_servers_numbers_and_survives_a_dead_server(w):
    key, data = shared(w, "report.md")
    w.server.shares[data["share_id"]]["views"] = 2
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is True and live["shares"][0]["views"] == 2
    w.server.fail.append(("GET", r"/shares$", 0, ""))
    w.server.fail.append(("GET", r"/shares$", 0, ""))
    w.server.fail.append(("GET", r"/shares$", 0, ""))
    dead = ok(w.req("publish.list", {"live": True}))["data"]
    assert dead["live"] is False and dead["shares"][0]["views"] == 0
    assert dead["why"] == "unreachable" and "why" not in live
    agent = w.served.client()
    res = agent.req("publish.list", {"live": True}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed"


@pytest.mark.parametrize("error", ["unknown_workspace", "owner_not_allowed"])
def test_list_live_says_not_allowed_when_the_server_refuses(w, error):
    """run17 P01: a hub that answers and refuses is online. The page must not say it cannot be reached."""
    shared(w, "report.md")
    for _ in range(3):
        w.server.fail.append(("GET", r"/shares$", 401, error))
    got = ok(w.req("publish.list", {"live": True}))["data"]
    assert got["live"] is False and got["why"] == "not_allowed"


def test_list_live_without_a_server_setting_answers_not_configured_not_an_error(bare):
    """The dashboard's Shares page always asks for the live list. Publish on but no server set up is a normal state
    (run 3 finding): the answer says `configured` false, it does not fail."""
    bare.addon()
    for args in ({"live": True}, {}):
        got = ok(bare.req("publish.list", args))["data"]
        assert got["configured"] is False and not got.get("live") and got["server"] is None
        if args:
            assert got["why"] == "local"
        assert got["shares"] == [] and got["staged"] == []


def test_an_agent_lists_only_its_own_ticket(w):
    key, data = shared(w, "report.md")
    uid2, key2 = w.ticket_with(**{"b.md": b"b"})
    mine = w.prepare(key2, "b.md")["staged_id"]
    agent = w.served.client()
    ok(agent.req("claim", {"ref": key2}, grant=w.served.grant, session=SESSION, idem=True))
    out = ok(agent.req("publish.list", {}, grant=w.served.grant, session=SESSION))["data"]
    assert out["shares"] == [] and [s["staged_id"] for s in out["staged"]] == [mine]


def test_discard_removes_a_staged_copy_and_an_agent_only_its_own(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    by_person = w.prepare(key, "report.md")["staged_id"]
    agent = w.served.client()
    ok(agent.req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    by_agent = ok(agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant,
                            session=SESSION))["data"]["staged_id"]  # fmt: skip
    res = agent.req("publish.discard", {"staged_id": by_person}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed" and w.stage_dir(by_person).exists()
    ok(agent.req("publish.discard", {"staged_id": by_agent}, grant=w.served.grant, session=SESSION))
    assert not w.stage_dir(by_agent).exists()
    ok(w.req("publish.discard", {"staged_id": by_person}))
    assert not w.stage_dir(by_person).exists()
    assert err(w.req("publish.discard", {"staged_id": by_person})) == "not_found"
    assert err(w.req("publish.discard", {"staged_id": "ps_x"})) == "invalid.input"


def test_a_discarded_copy_cannot_be_sent_even_if_it_was_signed(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    ok(w.req("publish.discard", {"staged_id": staged}))
    assert err(w.send(staged)) == "not_found"


# -- no secret anywhere ----------------------------------------------------------------------------------------------


def test_the_audit_file_names_ids_never_a_link_or_a_token(w):
    key, data = shared(w, "report.md")
    ok(w.req("publish.revoke", {"share_id": data["share_id"]}))
    audit = (Path(w.served.lab.state) / "host" / "audit-security.jsonl").read_text()
    events = [json.loads(line)["event"] for line in audit.splitlines()]
    assert {"publish.prepare", "publish.share", "publish.revoke"} <= set(events)
    token = LINK.fullmatch(data["link"]).group(2)
    assert token not in audit and "#t=" not in audit and data["link"] not in audit


def test_the_ops_are_all_declared_and_versioned():
    assert set(PUBLISH_OPS) == {
        "publish.stage_upload", "publish.prepare", "publish.send", "publish.revoke", "publish.list",
        "publish.discard", "publish.register",
        "publish.extend", "publish.password",
    }  # fmt: skip
    assert all(v == 1 for v in PUBLISH_OPS.values())
    assert sha(b"x").startswith("sha256:") and stored  # the helpers this module shares with the artifact tests


# -- review fixes (Opus on #685) -------------------------------------------------------------------------------------


def test_a_share_that_went_live_with_a_lost_commit_answer_can_be_revoked(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.lose.append(("POST", r"/commit$"))  # the server commits, the answer never arrives
    res = w.send(staged)
    assert err(res) == "publish.unreachable"
    (row,) = ok(w.req("publish.list", {}))["data"]["shares"]
    assert row["state"] == "sending" and "token" not in json.dumps(row)
    (share_id,) = w.server.shares
    assert w.server.shares[share_id]["state"] == "live"
    ok(w.req("publish.revoke", {"share_id": row["id"]}))
    assert w.server.shares[share_id]["state"] == "revoked"
    assert ok(w.req("publish.list", {}))["data"]["shares"][0]["state"] == "revoked"


def test_a_copy_that_may_be_live_is_not_discarded(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/files/report\.md$", 0, ""))
    assert err(w.send(staged)) == "publish.unreachable"
    res = w.req("publish.discard", {"staged_id": staged})
    assert err(res) == "publish.changed" and "revoke" in res["error"]["message"]
    assert w.stage_dir(staged).exists()


def test_only_the_ticket_owner_or_a_manager_may_sign_a_share(w, served):
    from types import SimpleNamespace

    uid, key = w.ticket_with(**{"report.md": REPORT})
    member = served.lab.add_member()
    store = served.lab.ws.other()
    try:
        state = store.state
        service = SimpleNamespace(store=SimpleNamespace(state=state))
        view = store.ticket(key)
        publishing._may_share(SimpleNamespace(service=service, viewer=served.lab.owner), view)  # an owner may
        with pytest.raises(HostError) as exc:
            publishing._may_share(SimpleNamespace(service=service, viewer=member.ref), view)
        assert exc.value.code == "role.denied"
        with pytest.raises(HostError):
            publishing._may_share(SimpleNamespace(service=service, viewer=None), view)
    finally:
        store.close()


def test_stage_room_is_checked_before_artifacts_are_read(w, monkeypatch):
    uid, key = w.ticket_with(**{"report.md": REPORT, "b.md": REPORT + b"b"})
    monkeypatch.setattr(publishing, "STAGE_BYTES", len(REPORT) + 5)
    monkeypatch.setattr(publishing, "PER_MINUTE", 100)
    w.prepare(key, "report.md")
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["b.md"]})) == "quota.host"


def test_an_agent_may_have_three_staged_copies_at_most(w, monkeypatch):
    monkeypatch.setattr(publishing, "PER_MINUTE", 100)
    uid, key = w.ticket_with(**{"report.md": REPORT})
    agent = w.served.client()
    ok(agent.req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    for _ in range(publishing.AGENT_STAGES):
        ok(
            agent.req(
                "publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant, session=SESSION
            )
        )
    res = agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant, session=SESSION)
    assert err(res) == "quota.host"
    w.prepare(key, "report.md")  # a person is not held to the agent limit


def test_names_that_differ_only_in_case_are_refused(w):
    uid, key = w.ticket_with(**{"Report.md": REPORT, "report.md": REPORT})
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["Report.md", "report.md"]})) == "invalid.input"


def test_an_old_approval_among_many_other_events_is_still_found_and_a_stale_one_is_not(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)

    def pile(store):
        for i in range(publishing.SCAN_CHUNK + 55):  # more notes than one scan chunk, all newer than the approval
            w.ws.tick()
            store.append(w.ws.person_event(w.ws.owner, uid, "log.added", text=f"note {i}"), log=uid)

    w.served.append(pile)
    assert ok(w.send(staged))["data"]["share_id"]
    # and a signature older than the window is refused, however many events came after it
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.served.append(pile)
    w.served.lab.ws.clock[0] += publishing.APPROVAL_VALID + 60
    assert err(w.send(staged)) == "publish.not_approved"


def test_a_member_or_viewer_cannot_sign_a_share_through_the_real_path(tmp_path, monkeypatch):
    from tests.host.live_helpers import world

    monkeypatch.setattr(publishing, "_hub_route", lambda service, **kw: None)  # about roles here, not the hub path
    wd = world(tmp_path, map_to="quinn")
    try:
        quinn = Human(wd, wd.quinn)
        w = World(wd, quinn, monkeypatch, join=False)  # quinn's own connection (only an owner joins a hub)
        w.addon()
        w.configure()
        uid, key = w.ticket_with(**{"report.md": REPORT})  # made by the owner's agent: quinn does not own it
        staged = w.prepare(key, "report.md")["staged_id"]  # anyone who sees the ticket may stage
        for role in ("member", "viewer"):
            if role == "viewer":
                wd.person_event("role.changed", person=wd.quinn.ref, role="viewer")
            assert err(quinn.prepare_res("publish.share", {"staged_id": staged})) == "role.denied", role
        wd.person_event("role.changed", person=wd.quinn.ref, role="maintainer")
        assert quinn.prepare("publish.share", {"staged_id": staged})["op"] == "publish.share"
    finally:
        pass
        wd.close()
        wd.lab.close()


def test_a_server_that_forgot_the_workspace_drops_the_cached_registration(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    ok(w.req("publish.register", {}))
    assert (w.base() / "registered.json").exists()
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/shares/[0-9a-f]{32}$", 401, "unknown_workspace"))
    res = w.send(staged)
    # P05: the workspace is registered again and the send is retried once, with no terminal step
    data = ok(res)["data"]
    assert LINK.fullmatch(data["link"])
    assert (w.base() / "registered.json").exists()


def test_a_hub_that_forgot_the_workspace_registers_again_once_and_retries(w):
    """The same through the hub's own refusal (``HubRefused unknown_workspace``), not only the server's 401."""
    uid, key = w.ticket_with(**{"report.md": REPORT})
    ok(w.req("publish.register", {}))
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    before = w.hub.ops().count("card_put")
    w.hub.error = "unknown_workspace"
    res = w.send(staged)  # still unknown after the one registration: refused in plain words, nothing is sent
    assert err(res) == "publish.server_refused" and "orch publish" not in res["error"]["message"]
    assert "orch-publish" not in res["error"]["message"]
    assert w.hub.ops().count("card_put") == before + 1, "registered exactly once more, then one retry"
    w.hub.error = None
    ok(w.send(staged))


# -- P05: a file picked on this computer -------------------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def pick(w, name: str, data: bytes, **kw: Any) -> dict[str, Any]:
    return ok(w.req("publish.stage_upload", {"name": name, "b64": b64(data)}, **kw))["data"]


def test_a_picked_file_is_staged_hashed_and_signed_like_a_ticket_file(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    up = pick(w, "notes.txt", b"hello there\n")
    assert re.fullmatch(r"pu_[0-9a-f]{32}", up["upload_id"]) and up["size"] == 12
    assert up["sha256"] == hashlib.sha256(b"hello there\n").hexdigest() and w.server.requests == []
    got = ok(w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"], "uploads": [up["upload_id"]]}))["data"]
    assert [(f["name"], f["size"], f["sha256"]) for f in got["files"]] == [
        ("report.md", len(REPORT), hashlib.sha256(REPORT).hexdigest()),
        ("notes.txt", 12, up["sha256"]),
    ]
    stage = w.stage_dir(got["staged_id"])
    assert (stage / "files" / "notes.txt").read_bytes() == b"hello there\n"
    assert oct((stage / "files" / "notes.txt").stat().st_mode & 0o777) == "0o600"
    assert "notes.txt" in got["words"] and got["staged_id"] in got["words"]  # the one signature covers it
    assert not (w.base() / "upload" / up["upload_id"]).exists()  # taken into the stage
    w.approve(got["staged_id"])
    data = ok(w.send(got["staged_id"]))["data"]
    (share_id,) = (data["share_id"],)
    assert w.server.shares[share_id]["files"]["notes.txt"] == b"hello there\n"
    assert w.server.shares[share_id]["files"]["report.md"] == REPORT


def test_only_a_picked_file_is_enough_for_a_share(w):
    uid, key = w.ticket_with()
    up = pick(w, "only.txt", b"x" * 100)
    got = ok(w.req("publish.prepare", {"ref": key, "uploads": [up["upload_id"]]}))["data"]
    assert [f["name"] for f in got["files"]] == ["only.txt"]
    assert err(w.req("publish.prepare", {"ref": key})) == "invalid.input"  # nothing picked at all


def test_a_picked_file_is_a_persons_act_never_an_agents(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    assert err(w.req("publish.stage_upload", {"name": "a.txt", "b64": b64(b"abc")}, agent=True)) == "op.not_allowed"
    up = pick(w, "p.txt", b"person")
    res = w.req("publish.prepare", {"ref": key, "artifacts": ["report.md"], "uploads": [up["upload_id"]]}, agent=True)
    assert err(res) == "op.not_allowed"
    assert (w.base() / "upload" / up["upload_id"]).exists() and not (w.base() / "stage").exists()


@pytest.mark.parametrize("name", ["../x.txt", ".hidden", "a/b.txt", "x" * 101, "bad\nname", " lead.txt"])
def test_a_picked_file_name_is_held_to_the_servers_rule(w, name):
    assert err(w.req("publish.stage_upload", {"name": name, "b64": b64(b"abc")})) == "invalid.input"
    assert not list((w.base() / "upload").glob("pu_*")) if (w.base() / "upload").exists() else True


def test_a_picked_file_has_a_size_limit_and_is_not_empty_or_garbled(w, monkeypatch):
    monkeypatch.setattr(publishing, "UPLOAD_BYTES", 100)
    monkeypatch.setitem(publishing._SCHEMAS["publish.stage_upload"]["properties"]["b64"], "maxLength", 1000)
    assert err(w.req("publish.stage_upload", {"name": "big.bin", "b64": b64(b"x" * 101)})) == "quota.host"
    assert err(w.req("publish.stage_upload", {"name": "e.bin", "b64": b64(b"")})) == "invalid.input"
    assert err(w.req("publish.stage_upload", {"name": "g.bin", "b64": "%%%%"})) == "invalid.input"
    pick(w, "ok.bin", b"x" * 100)


def test_pending_picks_are_bounded_and_the_oldest_makes_room(w, monkeypatch):
    monkeypatch.setattr(publishing, "MAX_UPLOADS", 3)
    ids = [pick(w, f"f{i}.txt", b"data%d" % i)["upload_id"] for i in range(5)]
    left = sorted(p.name for p in (w.base() / "upload").glob("pu_*"))
    assert len(left) == 3 and set(left) <= set(ids) and ids[-1] in left


def test_prepare_refuses_a_missing_or_changed_pick_and_a_name_clash(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    assert err(w.req("publish.prepare", {"ref": key, "uploads": ["pu_" + "0" * 32]})) == "not_found"
    clash = pick(w, "REPORT.md", b"other")
    both = {"ref": key, "artifacts": ["report.md"], "uploads": [clash["upload_id"]]}
    assert err(w.req("publish.prepare", both)) == "invalid.input"
    changed = pick(w, "c.txt", b"first")
    path = w.base() / "upload" / changed["upload_id"] / "data"
    path.write_bytes(b"swapd")  # same size, other bytes
    assert err(w.req("publish.prepare", {"ref": key, "uploads": [changed["upload_id"]]})) == "not_found"
    assert not (w.base() / "stage").exists() or not list((w.base() / "stage").iterdir())


def _retag(w, up: dict[str, Any], **change: Any) -> Path:
    d = w.base() / "upload" / up["upload_id"]
    meta = json.loads((d / "meta.json").read_text())
    meta.update(change)
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def test_a_pick_belongs_to_the_person_who_made_it(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    up = pick(w, "mine.txt", b"mine")
    d = _retag(w, up, by="someone-else")  # as if another person had made it
    assert err(w.req("publish.prepare", {"ref": key, "uploads": [up["upload_id"]]})) == "not_found"
    assert d.exists() and (d / "data").read_bytes() == b"mine"


def test_the_pick_cap_counts_only_the_callers_own_picks(w, monkeypatch):
    monkeypatch.setattr(publishing, "MAX_UPLOADS", 3)
    theirs = [pick(w, f"b{i}.txt", b"b%d" % i) for i in range(3)]
    for up in theirs:
        _retag(w, up, by="person-b")
    mine = [pick(w, f"a{i}.txt", b"a%d" % i)["upload_id"] for i in range(5)]
    left = {p.name for p in (w.base() / "upload").glob("pu_*")}
    assert {u["upload_id"] for u in theirs} <= left, "the other person's picks were not evicted"
    assert len(left - {u["upload_id"] for u in theirs}) == 3 and mine[-1] in left


@pytest.mark.parametrize("tamper", ["symlink", "hardlink", "mode", "other_workspace"])
def test_a_tampered_pick_is_refused_at_prepare(w, tamper):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    up = pick(w, "t.txt", b"tamper")
    d = w.base() / "upload" / up["upload_id"]
    if tamper == "symlink":
        (d / "data").unlink()
        (d / "data").symlink_to(w.base() / "upload" / "elsewhere")
        (w.base() / "upload" / "elsewhere").write_bytes(b"tamper")
    elif tamper == "hardlink":
        os.link(d / "data", w.base() / "upload" / "second-name")
    elif tamper == "mode":
        os.chmod(d / "data", 0o644)
    else:
        _retag(w, up, ws="another-workspace")
    assert err(w.req("publish.prepare", {"ref": key, "uploads": [up["upload_id"]]})) == "not_found"
    assert not (w.base() / "stage").exists() or not list((w.base() / "stage").iterdir())


def test_a_pick_needs_publish_to_be_on(bare):
    assert err(bare.req("publish.stage_upload", {"name": "a.txt", "b64": b64(b"abc")})) == "runner.disabled"


def test_the_hosts_settings_handler_checks_both_origins_whoever_asks(bare, human):
    """Condition C1: the host calls ``wire.check_origin`` itself (the shape pattern alone lets these through)."""
    bare.addon()
    for server, share in (
        ("https://a..example", SHARE),  # doubled dot: the pattern accepts it, check_origin does not
        (SERVER, "https://share.example:99999"),  # a port above 65535
    ):
        res = human.prepare_res("settings.set", {"set": {"publish": {"server": server, "share": share}}})
        assert not res["ok"] and res["error"]["code"] == "invalid.input", (server, share)
    good = human.prepare_res("settings.set", {"set": {"publish": {"server": SERVER, "share": SHARE}}})
    assert good["ok"]


def test_one_address_for_both_server_and_share_is_accepted(bare, human):
    """P06: the publish server's run mode serves uploads and links from one address, so equal addresses are valid."""
    bare.addon()
    res = human.prepare_res("settings.set", {"set": {"publish": {"server": SERVER, "share": SERVER}}})
    assert res["ok"]


def test_register_is_the_owners_act_three_a_minute_and_never_an_agents(w):
    assert err(w.req("publish.register", {}, agent=True)) in ("op.not_allowed", "human_only")
    for _ in range(3):
        ok(w.req("publish.register", {}))
    assert err(w.req("publish.register", {})) == "rate.limited"


def test_a_member_who_is_not_an_owner_cannot_register(tmp_path):
    from tests.host.live_helpers import world

    wd = world(tmp_path, map_to="quinn")
    try:
        assert err(wd.client().req("publish.register", {}, idem=True)) == "role.denied"
    finally:
        wd.close()
        wd.lab.close()


def test_the_review_tags_each_address_in_plain_words():
    from orch.host.signcontent import _reach

    assert _reach("http://127.0.0.1:8103") == "this computer" and _reach("http://localhost") == "this computer"
    assert (
        _reach("http://192.168.1.5:8103") == "the office network"
        and _reach("http://100.64.0.9") == "the office network"
    )
    assert _reach("http://172.16.0.1") == "the office network" and _reach("http://172.32.0.1") == "a public address"
    assert _reach("https://files.example.org:8443") == "a public address"


# -- P01 (run 14): a full-mode workspace publishes through the hub too; every refusal is in plain words ---------------


@pytest.fixture
def wfull(served, human, monkeypatch, keystore):  # noqa: F811
    world = World(served, human, monkeypatch, join=False)
    world.join("full")
    world.addon()
    world.configure()
    return world


def test_a_full_mode_workspace_publishes_through_the_hub_and_the_link_is_shown_once(wfull):
    w = wfull
    assert hubops.publish_route(w.served.service)["mode"] == "full"
    uid, key = w.ticket_with(**{"hello.txt": b"hello from orch"})
    staged = w.prepare(key, "hello.txt")["staged_id"]
    w.approve(staged)
    data = ok(w.send(staged))["data"]
    share_id, token = LINK.fullmatch(data["link"]).groups()
    assert data["shown_once"] is True and w.server.shares[share_id]["state"] == "live"
    assert "publish_out" in w.hub.ops() and "card_put" in w.hub.ops()  # through the hub, never a direct POST
    assert w.server.bad_signatures == []
    # the link is kept nowhere on this computer
    assert token.encode() not in w.everything_on_disk()
    # and it is listed (no token) for the person to stop
    rows = ok(w.req("publish.list", {}))["data"]["shares"]
    assert [r["id"] for r in rows] == [share_id] and "link" not in json.dumps(rows)


def test_a_full_mode_share_is_the_persons_signature_not_the_hubs_and_an_agent_cannot_send(wfull):
    w = wfull
    uid, key = w.ticket_with(**{"hello.txt": b"hello from orch"})
    staged = w.prepare(key, "hello.txt")["staged_id"]
    assert err(w.send(staged)) == "publish.not_approved"  # no signature yet: the route alone publishes nothing
    assert err(w.send(staged, agent=True)) in ("op.not_allowed", "publish.not_approved")
    assert w.server.shares == {}


def test_the_refusals_before_signing_are_plain_words_not_codes(served, human, monkeypatch):
    w = World(served, human, monkeypatch, join=False)
    human.run("hub.join", join_args())  # joined, never connected
    w.addon()
    w.configure()
    uid, key = w.ticket_with(**{"hello.txt": b"hello"})
    res = w.req("publish.prepare", {"ref": key, "artifacts": ["hello.txt"]})
    assert err(res) == "hub.not_joined"
    text = res["error"]["message"]
    assert "Settings" in text and "hub.state_mismatch" not in text and "orch hub" not in text


def test_a_hub_state_that_disagrees_with_the_log_is_refused_in_plain_words(wfull, monkeypatch):
    w = wfull
    real = hubops._load_state
    monkeypatch.setattr(hubops, "_load_state", lambda service: dataclasses.replace(real(service), mode="selective"))
    with pytest.raises(HostError) as e:
        hubops.publish_route(w.served.service)
    assert e.value.code == "hub.state_mismatch"
    assert "only selective" not in str(e.value) and "Settings" in str(e.value)
    # the binding's own mode must agree too (defence in depth): fail closed
    bad = dataclasses.replace(
        real(w.served.service),
        binding={**real(w.served.service).binding, "o": {**real(w.served.service).binding["o"], "mode": "selective"}},
    )
    monkeypatch.setattr(hubops, "_load_state", lambda service: bad)
    with pytest.raises(HostError) as e:
        hubops.publish_route(w.served.service)
    assert e.value.code == "hub.state_mismatch"


def test_the_signing_review_refuses_when_the_hub_is_joined_but_not_connected_and_signs_nothing(w, monkeypatch):
    uid, key = w.ticket_with(**{"hello.txt": b"hello"})
    staged = w.prepare(key, "hello.txt")["staged_id"]  # prepared while connected
    real = hubops._load_state
    monkeypatch.setattr(hubops, "_load_state", lambda service: dataclasses.replace(real(service), status="joining"))
    res = w.human.prepare_res("publish.share", {"staged_id": staged})
    assert err(res) == "hub.not_joined" and "Settings" in res["error"]["message"]
    assert "events" not in res.get("data", {}) and w.server.requests == []  # nothing to sign, nothing sent
    assert err(w.send(staged)) in ("publish.not_approved", "hub.not_joined")


# -- the display members of the share list (#1636) ---------------------------------------------------------------------


def _member_name(w) -> str:
    w.served.service.store.refresh()
    owners = [m.name for m in w.served.service.store.state.workspace.members.values() if m.role == "owner"]
    assert len(owners) == 1  # the test human is the one owner
    return owners[0]


def test_the_share_meta_names_the_signed_in_person_and_the_ticket(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    share_id = LINK.fullmatch(ok(w.send(staged))["data"]["link"]).group(1)
    meta = w.server.shares[share_id]["meta"]
    assert meta["by_kind"] == "person" and meta["by_name"] == _member_name(w)
    assert meta["label"] == w.served.service.store.ticket(uid).title


def test_an_agent_that_claims_to_be_a_person_is_stored_as_an_agent(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    agent = w.served.client()
    ok(agent.req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    for extra in ({"by_kind": "person"}, {"by_name": "The Owner"}, {"label": "Payroll"}):
        res = agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"], **extra},
                        grant=w.served.grant, session=SESSION)  # fmt: skip
        assert err(res) == "invalid.input", extra  # the schema takes no such member
    staged = ok(agent.req("publish.prepare", {"ref": key, "artifacts": ["report.md"]}, grant=w.served.grant,
                          session=SESSION))["data"]["staged_id"]  # fmt: skip
    w.approve(staged)
    share_id = LINK.fullmatch(ok(w.send(staged))["data"]["link"]).group(1)
    meta = w.server.shares[share_id]["meta"]
    assert meta["by_kind"] == "agent" and meta["by_name"] == _member_name(w)


def test_the_label_is_cleaned_and_capped():
    clean = publishing._display
    assert clean("  Q3 ‮report​ \n final ", 80) == "Q3 report final"  # bidi, zero width, white space
    assert clean("x" * 200, 80) == "x" * 80
    assert len(clean("é" * 100, 80).encode()) <= 80 and set(clean("é" * 100, 80)) == {"é"}
    assert clean("‮​\x07", 80) is None and clean(None, 80) is None


def test_a_server_that_refuses_the_label_gets_the_ticket_key_once(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.append(("PUT", r"/shares/[0-9a-f]{32}$", 400, "bad_label"))
    share_id = LINK.fullmatch(ok(w.send(staged))["data"]["link"]).group(1)
    assert w.server.shares[share_id]["meta"]["label"] == key
    puts = [r for r in w.server.requests if r["method"] == "PUT" and r["target"].endswith(f"shares/{share_id}")]
    assert len(puts) == 2 and json.loads(puts[0]["body"])["label"] != key


def test_a_label_refused_again_is_not_retried_forever(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    w.server.fail.extend([("PUT", r"/shares/[0-9a-f]{32}$", 400, "bad_label")] * 2)
    assert err(w.send(staged))
    assert len([r for r in w.server.requests if r["method"] == "PUT"]) == 2


def test_an_app_release_cannot_be_prepared_without_a_staged_release_the_host_built(w):
    """#1457: ``app.release`` names only a staged release; with none from the host module nothing is prepared, so nobody
    (and no agent) can obtain a signature over words of their own."""
    res = w.human.prepare_res("app.release", {"staged_id": "pa_" + "1" * 32})
    assert err(res) == "not_found"  # no such staged release; the words are never taken from the request
    res = w.human.prepare_res(
        "app.release", {"staged_id": "pa_" + "1" * 32, "text": "App approval: let an app go live"}
    )
    assert err(res) == "invalid.input"
    res = w.human.prepare_res("app.release", {"staged_id": "ps_" + "1" * 32})
    assert err(res) == "invalid.input"


# -- reserved approval lines are written only by their own click op (Opus review of #1781, F1) -----------------------

RESERVED = (approval.MARK, approval.APP_MARK)


@pytest.mark.parametrize("line", RESERVED)
def test_an_ordinary_person_note_cannot_start_with_a_reserved_approval_line(w, click, line):  # noqa: F811
    from orch.click.protocol import ClickError

    uid, key = w.ticket_with(**{"report.md": REPORT})
    for text in (line, line + "\nTicket: " + key + "\nStaged copy: ps_" + "a" * 32):
        with pytest.raises(ClickError):
            click.prepare("log", {"ref": key, "text": text})
    ok_note = click.prepare("log", {"ref": key, "text": "a normal note about the " + line.lower()})
    assert ok_note["request_id"]  # the same words later in a note are not a reserved first line


@pytest.mark.parametrize("line", RESERVED)
def test_an_agent_note_cannot_start_with_a_reserved_approval_line(w, line):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    ok(w.served.client().req("claim", {"ref": key}, grant=w.served.grant, session=SESSION, idem=True))
    res = w.req("log", {"ref": key, "text": line + "\nTicket: " + key}, agent=True, idem=True)
    assert err(res) == "invalid.input"
    ok(w.req("log", {"ref": key, "text": "all fine"}, agent=True, idem=True))


def test_the_signer_refuses_a_reserved_line_from_any_other_op(w):
    """The signer's own check, independent of the registry: a note whose text starts with a reserved line is accepted
    only under its own op, whatever else is right about it."""
    from orch.host import signer

    uid, key = w.ticket_with(**{"report.md": REPORT})
    got = w.prepare(key, "report.md")
    prepared = w.human.prepare("publish.share", {"staged_id": got["staged_id"]})
    w.human.check(prepared)  # its own op: accepted
    for op, text in (
        ("log", prepared["events"][0]["text"]),  # the share approval written as an ordinary note
        ("app.release", prepared["events"][0]["text"]),  # one approval kind under the other's op
        ("publish.share", approval.APP_MARK + "\nTicket: " + key),
        ("log", approval.APP_MARK + "\nTicket: " + key),
        ("move", approval.APP_MARK + "\nTicket: " + key),
    ):
        bad = json.loads(json.dumps(prepared))
        bad["op"], bad["events"][0]["text"] = op, text
        with pytest.raises(signer.SignerRefused):
            w.human.check(bad)
    on_workspace = json.loads(json.dumps(prepared))
    on_workspace["op"], on_workspace["log"] = "app.release", "workspace"
    on_workspace["events"][0]["text"] = approval.APP_MARK + "\nTicket: " + key
    with pytest.raises(signer.SignerRefused):
        w.human.check(on_workspace)  # notes on the workspace log are refused too


def test_only_a_clear_refusal_marks_a_share_failed_and_old_failed_records_are_pruned():
    from datetime import UTC, datetime

    from orch.host import publishing as P
    from orch.publishclient import PublishError

    for status, want in (
        (401, True),
        (403, True),
        (404, True),
        (408, False),
        (409, False),
        (429, False),
        (500, False),
        (0, False),
    ):
        assert P._refused_outright(PublishError("x", status)) is want, status  # noqa: SLF001
    now = 1_800_000_000.0
    iso = lambda t: datetime.fromtimestamp(t, UTC).isoformat()  # noqa: E731
    rows = [
        {"id": "old", "state": "failed", "created_at": iso(now - 8 * 86400)},
        {"id": "live", "state": "live", "created_at": iso(now - 99 * 86400)},
    ]
    rows += [{"id": f"f{i}", "state": "failed", "created_at": iso(now - 60)} for i in range(60)]
    out = P._prune_failed(rows, now)  # noqa: SLF001
    ids = [r["id"] for r in out]
    assert "old" not in ids and "live" in ids
    assert sum(r["state"] == "failed" for r in out) == 50 and ids[-1] == "f59"


# -- P03: files of the ticket's own work folder --------------------------------------------------------------------


def work_dir(w, key: str) -> Path:
    path = Path(w.served.lab.root) / ".orch-work" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_prepare_stages_a_work_folder_file_and_the_one_signature_covers_it(w):
    uid, key = w.ticket_with()
    folder = work_dir(w, key)
    (folder / "hello.txt").write_bytes(b"hello from work\n")
    got = ok(w.req("publish.prepare", {"ref": key, "workfiles": ["hello.txt"]}))["data"]
    assert [(f["name"], f["size"], f["sha256"]) for f in got["files"]] == [
        ("hello.txt", 16, hashlib.sha256(b"hello from work\n").hexdigest())
    ]
    stage = w.stage_dir(got["staged_id"])
    assert (stage / "files" / "hello.txt").read_bytes() == b"hello from work\n"
    assert "hello.txt" in got["words"] and w.server.requests == []
    w.approve(got["staged_id"])
    data = ok(w.send(got["staged_id"]))["data"]
    assert w.server.shares[data["share_id"]]["files"]["hello.txt"] == b"hello from work\n"


def test_a_work_file_cannot_escape_the_folder_or_be_a_link_or_empty_or_big(w, monkeypatch):
    uid, key = w.ticket_with()
    folder = work_dir(w, key)
    root = Path(w.served.lab.root)
    outside = root / "secret.txt"
    outside.write_bytes(b"outside")  # a separate file with ONE link: only O_NOFOLLOW can refuse the symlink to it
    (root / "otherdir").mkdir()
    (folder / "link.txt").symlink_to(outside)
    (folder / "dangling.txt").symlink_to(root / "nothing-here")
    (folder / "dirlink").symlink_to(root / "otherdir")
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.txt").write_bytes(b"deep")
    (folder / "empty.txt").write_bytes(b"")
    twin = root / "twin.txt"
    twin.write_bytes(b"twin")
    os.link(twin, folder / "hard.txt")
    (folder / "big.bin").write_bytes(b"x" * 50)
    for bad in ("../secret.txt", "/etc/passwd", "sub/deep.txt", "..", ".hidden", "a\\b"):
        assert err(w.req("publish.prepare", {"ref": key, "workfiles": [bad]})) == "invalid.input"
    for refused in ("link.txt", "dangling.txt", "dirlink", "hard.txt", "sub", "nope.txt"):
        assert err(w.req("publish.prepare", {"ref": key, "workfiles": [refused]})) == "not_found", refused
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["empty.txt"]})) == "invalid.input"
    monkeypatch.setitem(approval.LIMITS, "file_bytes", 10)
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["big.bin"]})) == "quota.host"
    monkeypatch.setitem(approval.LIMITS, "file_bytes", 25 * 1024 * 1024)
    monkeypatch.setattr(publishing, "STAGE_BYTES", 10)  # no room left in the stage
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["big.bin"]})) == "quota.host"
    assert not (w.base() / "stage").exists() or not list((w.base() / "stage").glob("ps_*"))


def _no_stage(w) -> bool:
    return not (w.base() / "stage").exists() or not list((w.base() / "stage").glob("ps_*"))


def test_through_the_hub_a_file_over_one_link_frame_is_refused_at_prepare_and_nothing_is_staged(w):
    from orch.hubclient.publish import HUB_FILE_MAX

    uid, key = w.ticket_with()
    (work_dir(w, key) / "big.bin").write_bytes(b"x" * (HUB_FILE_MAX + 1))
    res = w.req("publish.prepare", {"ref": key, "workfiles": ["big.bin"]})
    assert err(res) == "quota.host"
    limit = approval.human_size(HUB_FILE_MAX)
    assert res["error"]["message"].startswith(f"Files over {limit} can't go through the hub yet.")
    assert f"big.bin is {approval.human_size(HUB_FILE_MAX + 1)}" in res["error"]["message"]
    assert _no_stage(w) and w.server.requests == []


def test_through_the_hub_a_file_of_exactly_hub_file_max_passes_prepare(w):
    from orch.hubclient.publish import HUB_FILE_MAX

    uid, key = w.ticket_with()
    (work_dir(w, key) / "edge.bin").write_bytes(b"x" * HUB_FILE_MAX)
    got = ok(w.req("publish.prepare", {"ref": key, "workfiles": ["edge.bin"]}))["data"]
    assert got["files"][0]["size"] == HUB_FILE_MAX


def test_through_the_hub_a_picked_file_over_the_frame_is_refused_at_prepare(w, monkeypatch):
    uid, key = w.ticket_with()
    monkeypatch.setattr("orch.hubclient.publish.HUB_FILE_MAX", 100)  # a pick is at most 1 MiB: shrink the frame instead
    monkeypatch.setattr(publishing, "PER_MINUTE", 100)
    pick = ok(w.req("publish.stage_upload", {"name": "p.bin", "b64": base64.b64encode(b"x" * 101).decode()}))["data"]
    res = w.req("publish.prepare", {"ref": key, "uploads": [pick["upload_id"]]})
    assert err(res) == "quota.host" and "can't go through the hub yet" in res["error"]["message"]
    assert _no_stage(w)


def test_without_a_hub_route_a_one_megabyte_file_passes_prepare(w, monkeypatch):
    uid, key = w.ticket_with()
    (work_dir(w, key) / "mb.bin").write_bytes(b"x" * 1_000_000)
    monkeypatch.setattr(publishing, "_hub_route", lambda service, **kw: None)  # direct: no frame, no hub limit
    got = ok(w.req("publish.prepare", {"ref": key, "workfiles": ["mb.bin"]}))["data"]
    assert got["files"][0]["size"] == 1_000_000


def test_a_fifo_in_the_work_folder_is_not_read_and_does_not_hang(w):
    uid, key = w.ticket_with()
    os.mkfifo(work_dir(w, key) / "pipe.txt")
    done = []

    def run() -> None:
        done.append(err(w.req("publish.prepare", {"ref": key, "workfiles": ["pipe.txt"]})))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    assert done == ["not_found"]


def test_a_linked_work_folder_is_refused(w):
    uid, key = w.ticket_with()
    real = Path(w.served.lab.root) / "elsewhere"
    real.mkdir()
    (real / "x.txt").write_bytes(b"x")
    (Path(w.served.lab.root) / ".orch-work").mkdir(exist_ok=True)
    (Path(w.served.lab.root) / ".orch-work" / key).symlink_to(real)
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["x.txt"]})) == "not_found"


def test_work_files_are_a_persons_act_and_another_tickets_folder_is_not_reachable(w):
    uid, key = w.ticket_with()
    (work_dir(w, key) / "a.txt").write_bytes(b"mine")
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["a.txt"]}, agent=True)) == "op.not_allowed"
    uid2, key2 = w.ticket_with()
    (work_dir(w, key2) / "a.txt").write_bytes(b"the other ticket's file")
    got = ok(w.req("publish.prepare", {"ref": key, "workfiles": ["a.txt"]}))["data"]
    assert [(f["size"], f["sha256"]) for f in got["files"]] == [(4, hashlib.sha256(b"mine").hexdigest())]
    assert (w.stage_dir(got["staged_id"]) / "files" / "a.txt").read_bytes() == b"mine"
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["a.txt", "A.txt"]})) == "invalid.input"


def test_only_who_may_delete_the_folder_may_share_or_list_its_files(tmp_path, monkeypatch):
    from tests.host.live_helpers import world

    monkeypatch.setattr(publishing, "_hub_route", lambda service, **kw: None)
    wd = world(tmp_path, map_to="quinn")
    try:
        w = World(wd, Human(wd, wd.quinn), monkeypatch, join=False)  # quinn: a member who sees the ticket
        w.addon()
        w.configure()
        uid, key = w.ticket_with()  # made by the owner's agent: quinn is neither its owner nor an assignee
        (work_dir(w, key) / "a.txt").write_bytes(b"abc")
        assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["a.txt"]})) == "op.not_allowed"
        assert "items" not in ok(w.req("workdir.get", {"ticket": key, "list": True}))["data"]
        wd.person_event("role.changed", person=wd.quinn.ref, role="maintainer")
        assert ok(w.req("workdir.get", {"ticket": key, "list": True}))["data"]["items"] == [
            {"name": "a.txt", "bytes": 3}
        ]
        assert [
            f["name"] for f in ok(w.req("publish.prepare", {"ref": key, "workfiles": ["a.txt"]}))["data"]["files"]
        ] == ["a.txt"]
    finally:
        wd.close()
        wd.lab.close()


def test_a_work_file_owned_by_someone_else_is_neither_listed_nor_read(w, monkeypatch):
    uid, key = w.ticket_with()
    (work_dir(w, key) / "mine.txt").write_bytes(b"abc")
    ok(w.req("publish.prepare", {"ref": key, "workfiles": ["mine.txt"]}))
    import orch.layout as layout_mod

    monkeypatch.setattr(layout_mod, "_file_owner", lambda: os.getuid() + 1)  # the file's owner is "someone else" now
    assert err(w.req("publish.prepare", {"ref": key, "workfiles": ["mine.txt"]})) == "not_found"


def test_the_share_cap_is_named_when_it_is_what_leaves_no_room(w, monkeypatch):
    uid, key = w.ticket_with()
    (work_dir(w, key) / "f.bin").write_bytes(b"x" * 50)
    monkeypatch.setitem(approval.LIMITS, "share_bytes", 10)
    res = w.req("publish.prepare", {"ref": key, "workfiles": ["f.bin"]})
    assert err(res) == "quota.host" and "a share holds at most" in json.dumps(res)


@pytest.mark.parametrize(
    "exc,why",
    [
        (HubRefused("unavailable"), "unreachable"),
        (HubRefused("hub_unreachable"), "unreachable"),
        (HubRefused("unknown_workspace"), "not_allowed"),
        (HubRefused("owner_not_allowed"), "not_allowed"),
        (PublishError("network"), "unreachable"),
        (PublishError("unavailable", 503), "unreachable"),
        (PublishError("unknown_workspace", 401), "not_allowed"),
        (PublishError("rate_limited", 429), "unreachable"),
        (PublishError("bad_signature", 401), "unreachable"),
        (PublishError("bad_nonce", 401), "unreachable"),
        (HubRefused("timeout"), "unreachable"),
        (HubRefused("workspace_blocked"), "not_allowed"),
    ],
)
def test_why_the_servers_numbers_are_missing(exc, why):
    from orch.host.publishing import _why_not_live

    assert _why_not_live(exc) == why


# -- #1838: where the hub says to publish: provenance, inert after leave, moved ---------------------------------------


NEW_SHARE = "http://127.0.0.1:8105"


def hold(w, seq=1, server=SERVER, share=SHARE, **kw):
    """The hub's signed ``publish_where`` as the host keeps it, through the one verifier (join and ``hub_where``)."""
    from orch.hubclient import publish_held
    from tests.hub.fake import signed_publish_where

    service = w.served.service
    with hubops._MU:
        state = hubops._load_state(service)
        code, _ = publish_held.offer(
            state, signed_publish_where(seq=seq, server=server, share=share, **kw), int(service.clock() * 1000)
        )
        assert code is None, code
        hubops._write_state(service, state)


def sign_settings(w, server=SERVER, share=SHARE):
    w.human.run("settings.set", {"set": {"publish": {"server": server, "share": share}}})


def from_hub_file(w):
    path = w.base() / "from_hub.json"
    return json.loads(path.read_text()) if path.exists() else None


def status_of(w):
    return ok(w.served.client().req("hub.status", {}, idem=False))["data"]


def staged_and_approved(w):
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    return staged


def test_signing_the_hubs_values_records_where_they_came_from_in_an_unsigned_file_next_to_the_config(w):
    hold(w)
    assert from_hub_file(w) is None and status_of(w)["publish_from"] is None  # configured by hand so far
    sign_settings(w)
    assert from_hub_file(w) == {"hub_id": HUB_ID, "hub_name": None, "server": SERVER, "share": SHARE}
    assert status_of(w)["publish_from"] == {"hub_id": HUB_ID, "hub_name": None}
    assert oct((w.base() / "from_hub.json").stat().st_mode & 0o777) == "0o600"
    # the signed setting itself keeps its shape: {server, share}, nothing about a hub
    assert hubops.workspace_dev(w.served.service)
    assert dict(w.served.service.store.state.workspace.settings["publish"]) == {"server": SERVER, "share": SHARE}


def test_values_that_are_not_the_hubs_record_nothing_and_clear_an_earlier_record(w):
    hold(w)
    sign_settings(w, share=NEW_SHARE)  # the person's own choice
    assert from_hub_file(w) is None
    sign_settings(w)
    assert from_hub_file(w) is not None
    sign_settings(w, share=NEW_SHARE)  # re-confirming with anything else ends the hub's claim
    assert from_hub_file(w) is None and status_of(w)["publish_from"] is None


def test_the_same_values_with_no_verified_object_held_record_no_provenance(w):
    sign_settings(w)  # the hub holds nothing: the values alone make no provenance
    assert from_hub_file(w) is None


def test_after_leave_the_settings_that_came_from_the_hub_are_inert_until_reconfirmed(w):
    hold(w)
    sign_settings(w)
    staged = staged_and_approved(w)
    uid, key = w.ticket_with(**{"other.md": REPORT})
    w.human.run("hub.leave", {})
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "hub_left"}
    assert "Sharing went through your hub. Connect again, or set a sharing server." in res["error"]["message"]
    assert w.server.requests == [] and w.stage_dir(staged).exists()  # nothing left the computer, the copy waits
    reg = w.req("publish.register", {})
    assert err(reg) == "publish.server_refused" and reg["error"]["detail"] == {"reason": "hub_left"}
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is False and live["why"] == "hub_left"
    prep = w.req("publish.prepare", {"ref": key, "artifacts": ["other.md"]})
    assert err(prep) == "publish.server_refused" and prep["error"]["detail"] == {"reason": "hub_left"}
    # re-confirming (signing publish settings again) clears it: the plain "no hub" answers are back
    sign_settings(w)
    assert from_hub_file(w) is None
    assert ok(w.req("publish.list", {"live": True}))["data"]["why"] == "local"
    assert err(w.send(staged)) == "hub.not_joined"


def test_the_inert_text_names_the_hub_when_the_hub_told_its_name(w):
    service = w.served.service
    with hubops._MU:
        state = hubops._load_state(service)
        state.hub_name = "Acme hub"
        hubops._write_state(service, state)
    hold(w)
    sign_settings(w)
    staged = staged_and_approved(w)
    w.human.run("hub.leave", {})
    msg = w.send(staged)["error"]["message"]
    assert msg == "Sharing went through your hub Acme hub. Connect again, or set a sharing server."


def test_settings_typed_by_hand_are_not_inert_after_leave(w):
    staged = staged_and_approved(w)
    w.human.run("hub.leave", {})
    assert err(w.send(staged)) == "hub.not_joined"  # as before #1838: no provenance, no new reason


def test_when_the_hub_moves_the_address_the_row_says_hub_moved_and_a_send_is_refused(w):
    hold(w)
    sign_settings(w)
    staged = staged_and_approved(w)
    assert status_of(w)["publish_moved"] is None
    hold(w, seq=2, share=NEW_SHARE)
    moved = {"server": SERVER, "share": NEW_SHARE}
    assert status_of(w)["publish_moved"] == moved and status_of(w)["publish_where"]["share"] == NEW_SHARE
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is False and live["why"] == "hub_moved" and live["hub_moved"] == moved
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "hub_moved"}
    assert res["error"]["message"] == "Sharing paused: your hub now shares through 127.0.0.1:8105."
    assert w.server.requests == []
    uid, key = w.ticket_with(**{"other.md": REPORT})
    assert err(w.req("publish.prepare", {"ref": key, "artifacts": ["other.md"]})) == "publish.server_refused"
    # signing the new values is the way out
    sign_settings(w, share=NEW_SHARE)
    assert status_of(w)["publish_moved"] is None and from_hub_file(w)["share"] == NEW_SHARE


def test_a_revoked_op_cert_ends_the_moved_state_with_the_suggestion(w):
    hold(w)
    sign_settings(w)
    hold(w, seq=2, share=NEW_SHARE)
    assert status_of(w)["publish_moved"] is not None
    service = w.served.service
    with hubops._MU:
        state = hubops._load_state(service)
        state.revoked_op_certs.append(state.card["op_cert"]["o"]["cert_id"])
        hubops._write_state(service, state)
    got = status_of(w)
    assert got["publish_where"] is None and got["publish_moved"] is None
    assert from_hub_file(w) is not None  # the record stays; it is only the hub's word that is gone


def test_a_setting_signed_in_another_spelling_of_the_held_origins_is_the_same_address_and_not_moved(w):
    from orch.host import hubpublish

    hold(w, server="https://pub.example.org", share="https://apps.example.org")
    service = w.served.service
    canonical = {
        "hub_id": HUB_ID, "hub_name": None, "server": "https://pub.example.org", "share": "https://apps.example.org",
    }  # fmt: skip
    sign_settings(w, server="https://pub.example.org:443", share="https://apps.example.org")  # the schema's spelling
    assert from_hub_file(w) == canonical  # the record keeps the canonical values, not the person's spelling
    got = status_of(w)
    assert got["publish_from"] == {"hub_id": HUB_ID, "hub_name": None} and got["publish_moved"] is None
    publishing._hub_guard(service, sending=True)  # not hub_moved, not hub_left: nothing raised
    # the event schema only lets the default port through as another spelling; the comparison is canonical anyway
    assert hubpublish._same("https://Pub.Example.org.:443", "https://pub.example.org", True)  # noqa: SLF001
    assert not hubpublish._same("https://pub.example.org:8443", "https://pub.example.org", True)  # noqa: SLF001
    # a real move is still seen, and compared canonically too
    hold(w, seq=2, server="https://pub.example.org", share="https://new.example.org")
    assert status_of(w)["publish_moved"] == {"server": "https://pub.example.org", "share": "https://new.example.org"}
    # a record written with the raw spelling (as before) is read the same way
    (w.base() / "from_hub.json").write_text(json.dumps({**canonical, "server": "https://Pub.Example.org:443"}))
    assert status_of(w)["publish_from"] is not None


def test_a_value_in_force_that_cannot_be_canonicalised_is_compared_as_written_so_the_guards_stay_on(w, monkeypatch):
    from orch.host import hubpublish

    hold(w)
    sign_settings(w)
    service = w.served.service
    record = from_hub_file(w)
    assert hubpublish.from_hub(service) == record
    monkeypatch.setattr(hubpublish, "_canon", lambda value, dev: None)  # e.g. a ``.lan`` set before the rule
    assert hubpublish.from_hub(service) == record  # raw equality: not None, not failing open
    w.human.run("hub.leave", {})
    assert hubpublish.left(service) == record  # hub_left still applies
    (w.base() / "from_hub.json").write_text(json.dumps({**record, "share": "https://other.example.org"}))
    assert hubpublish.from_hub(service) is None  # a record that names other values is not the settings' record


def test_the_guard_runs_once_at_plan_time_and_once_at_execution_and_status_reads_each_thing_once(w, monkeypatch):
    from orch.host import hubpublish

    hold(w)
    sign_settings(w)
    staged = staged_and_approved(w)
    calls = {"left": 0, "held": 0, "from_hub": 0}
    for name, real in (("left", hubpublish.left), ("held", hubpublish.held), ("from_hub", hubpublish.from_hub)):

        def counted(*a, _name=name, _real=real, **k):
            calls[_name] += 1
            return _real(*a, **k)

        monkeypatch.setattr(hubpublish, name, counted)
    # `left` runs from_hub once itself; moved() (sending) runs held once more
    res = w.send(staged)
    assert ok(res)["data"]
    assert calls["left"] == 2  # _plan's guard and the executor's: _hub_route does not run it a third time
    calls.update(held=0, from_hub=0)
    status_of(w)
    assert calls["held"] == 1 and calls["from_hub"] == 1  # status_fields: one read of each, passed down to moved()


def test_a_plan_made_before_a_leave_or_a_move_is_refused_by_the_guard_when_it_runs(w):
    from types import SimpleNamespace

    hold(w)
    sign_settings(w)
    service = w.served.service
    call = SimpleNamespace(service=service)
    hold(w, seq=2, share=NEW_SHARE)  # the hub moved on after the plan was made
    service.store.refresh()
    for op, reason in (("publish.send", "hub_moved"),):
        with pytest.raises(HostError) as e:
            publishing._run(call, {"op": op})
        assert e.value.code == "publish.server_refused" and e.value.detail == {"reason": reason}
    w.human.run("hub.leave", {})  # and then the workspace left it
    service.store.refresh()
    for op in ("publish.send", "publish.register"):
        with pytest.raises(HostError) as e:
            publishing._run(call, {"op": op})
        assert e.value.code == "publish.server_refused" and e.value.detail == {"reason": "hub_left"}
    assert w.server.requests == []  # the executor never reached the network


def test_the_run_time_guard_covers_send_and_register_and_not_the_ops_that_act_on_links_already_out(w, monkeypatch):
    """#1840 R1: ``_run`` re-guards ``publish.send`` (``sending=True``) and ``publish.register`` (``sending=False``) and
    never revoke, extend, password or list: stopping a link that is out must stay possible after a leave or a move."""
    from types import SimpleNamespace

    hold(w)
    sign_settings(w)
    service = w.served.service
    call = SimpleNamespace(service=service)
    guarded: list[tuple[str, bool]] = []
    real_guard = publishing._hub_guard

    def spy(svc, *, sending=False):
        guarded.append(("send" if sending else "other", sending))
        return real_guard(svc, sending=sending)

    monkeypatch.setattr(publishing, "_hub_guard", spy)
    ran: list[str] = []
    for name in ("_register", "_send", "_revoke", "_extend", "_set_password", "_live"):
        monkeypatch.setattr(publishing, name, lambda c, p, _n=name: ran.append(_n) or {})
    w.human.run("hub.leave", {})  # the hub is gone: send and register must now be refused at run time ...
    service.store.refresh()
    for op in ("publish.send", "publish.register"):
        with pytest.raises(HostError) as e:
            publishing._run(call, {"op": op})
        assert e.value.detail == {"reason": "hub_left"}
    assert ran == [] and guarded == [("send", True), ("other", False)]
    guarded.clear()
    for op, fn in (
        ("publish.revoke", "_revoke"),
        ("publish.extend", "_extend"),
        ("publish.password", "_set_password"),
        ("publish.list", "_live"),
    ):  # ... and these run on, with no guard at all
        publishing._run(call, {"op": op})
        assert ran[-1] == fn
    assert guarded == []
