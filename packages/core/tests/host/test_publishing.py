"""P5: the Publish add-on on the host (design-publish-addon §2.2, §3): ``publish.prepare`` / ``share`` / ``send`` /
``revoke`` / ``list`` / ``discard`` / ``register`` end to end on a real served workspace, against a fake server that
speaks orch-publish's routes (through a hub: there is no direct path, V3-D36).

The invariants under test (I1, I2, I12 and the signing rules): nothing leaves without a person's signature over the
exact words; an agent only prepares; the link is returned once and kept nowhere; the bytes sent are the staged bytes;
every request is signed with the workspace key for the configured host."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from orch import crypto
from orch.crypto import suite as S
from orch.host import hubops, hubpolicy, publishing, signcontent
from orch.host.publishing import PUBLISH_OPS
from orch.publishclient import approval, flow
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.human_helpers import Human
from tests.host.test_artifact_get import stored
from tests.host.test_blobs import add, sha, upload
from tests.host.test_click import Click, click  # noqa: F401
from tests.host.test_hub_ops import HUB_ID, connect_args, join_args
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

    def join(self) -> None:
        self.human.run("hub.join", join_args())
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
    ok(agent.req("log", {"text": staged["words"], "ref": key}, grant=w.served.grant, session=SESSION, idem=True))
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
    w.hub.error = None
    ok(w.send(staged))  # the admin allowed it; the same copy goes


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
    agent = w.served.client()
    res = agent.req("publish.list", {"live": True}, grant=w.served.grant, session=SESSION)
    assert err(res) == "op.not_allowed"


def test_list_live_without_a_server_setting_answers_not_configured_not_an_error(bare):
    """The dashboard's Shares page always asks for the live list. Publish on but no server set up is a normal state
    (run 3 finding): the answer says `configured` false, it does not fail."""
    bare.addon()
    for args in ({"live": True}, {}):
        got = ok(bare.req("publish.list", args))["data"]
        assert got["configured"] is False and not got.get("live") and got["server"] is None
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

    from orch.host.errors import HostError

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
    assert _reach("http://192.168.1.5:8103") == "office network" and _reach("http://100.64.0.9") == "office network"
    assert _reach("http://172.16.0.1") == "office network" and _reach("http://172.32.0.1") == "public address"
    assert _reach("https://files.example.org:8443") == "public address"


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
