"""The hub's admin read client against a fake orch-publish that checks every request with the contract's own
`check_admin_read` (the oracle), plus the `releases` CLI."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orch_hub import cli
from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.keys import HubKeys
from orch_hub.link import Registry
from orch_hub.publish import AdminReadClient, AdminReadError

NOW = G.NOW
ROW = {
    "workspace_id": G.WS_A,
    "id": "ab" * 16,
    "type": "link",
    "kind": "secret",
    "state": "live",
    "created_ms": NOW - 1000,
    "expires_ms": NOW + 86_400_000,
    "bytes": 12_345,
    "label": "Q3 report",
    "ticket": None,
    "by_kind": "person",
    "by_name": "Ana",
    "views": 3,
    "has_password": False,
}


def state_of(mode="full", extra_ws=()):
    cards = {G.WS_A: {"wsk_pub": G.K.b("wsk_a"), "owner_person_id": G.P_ALICE}}
    b = H.verify_binding(G.binding(mode=mode), workspace_id=G.WS_A, wsk_pub=G.K.pub["wsk_a"], now_ms=NOW, dev_mode=True)
    return {
        "cards": cards,
        "bindings": {G.WS_A: H.apply_binding(None, b)},
        "hub_cards": {G.HUB_ID: G.card()},
        "revoked_op_certs": [],
    }


class FakePublish:
    """GET /v2/hub/releases, verified with the contract; answers `rows` (or `override`)."""

    def __init__(self, st, rows=(ROW,), override=None, clock=lambda: NOW):
        self.st, self.rows, self.override, self.seen, self.clock = st, list(rows), override, [], clock
        outer = self

        class H_(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                req = {"method": "GET", "target": self.path, "headers": dict(self.headers)}
                outer.seen.append(req)
                try:
                    r = H.check_admin_read(req, outer.st, now_ms=outer.clock(), dev_mode=True)
                except H.Refused as e:
                    return self._send({"error": e.code}, 401 if e.code != "not_bound" else 403)
                if outer.override is not None:
                    return self._send(*outer.override)
                self._send({"rows": [x for x in outer.rows if x["workspace_id"] in r["workspaces"]]})

            def _send(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H_)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=lambda: self.srv.serve_forever(0.02), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    made = []

    def make(**kw):
        f = FakePublish(**kw)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def client(url, **kw):
    return AdminReadClient(url, KS.OpSigner(G.K.priv["hk_op"]), G.HUB_ID, now_ms=lambda: NOW, **kw)


def test_signed_request_passes_the_contract_check_and_returns_the_rows(fake):
    f = fake(st=state_of())
    assert client(f.url).published() == [ROW]
    (req,) = f.seen
    assert req["method"] == "GET" and req["target"] == "/v2/hub/releases"
    assert set(req["headers"]) >= {"Orch-Hub", "Orch-Hub-Ts", "Orch-Hub-Signature"}


def test_one_workspace(fake):
    f = fake(st=state_of())
    assert client(f.url).published(G.WS_A) == [ROW]
    assert f.seen[0]["target"] == f"/v2/hub/releases?workspace={G.WS_A}"


def test_a_selective_workspace_is_out_of_scope_at_the_server(fake):
    f = fake(st=state_of(mode="selective"))
    assert client(f.url).published() == []
    with pytest.raises(AdminReadError) as e:
        client(f.url).published(G.WS_A)
    assert e.value.code == "not_bound"


@pytest.mark.parametrize("ws", ["ABC", G.WS_A.upper(), "../x", G.WS_A + "&x=1", "%30" + G.WS_A[2:]])
def test_a_workspace_that_is_not_canonical_never_leaves_the_hub(fake, ws):
    f = fake(st=state_of())
    with pytest.raises(AdminReadError) as e:
        client(f.url).published(ws)
    assert e.value.code == "malformed" and f.seen == []


def test_server_refusals_surface_as_their_code(fake):
    f = fake(st=state_of(), clock=lambda: NOW + 500_000)
    with pytest.raises(AdminReadError) as e:
        client(f.url).published()
    assert e.value.code == "stale_timestamp"
    f2 = fake(st={**state_of(), "revoked_op_certs": [G.OP_CERT_ID]})
    with pytest.raises(AdminReadError) as e:
        client(f2.url).published()
    assert e.value.code == "unknown_hub"
    f3 = fake(st={**state_of(), "hub_cards": {}})
    with pytest.raises(AdminReadError) as e:
        client(f3.url).published()
    assert e.value.code == "unknown_hub"


def test_a_wrong_signer_is_refused_by_the_server(fake):
    f = fake(st=state_of())
    c = AdminReadClient(f.url, KS.OpSigner(G.K.priv["hk_op_rogue"]), G.HUB_ID, now_ms=lambda: NOW)
    with pytest.raises(AdminReadError) as e:
        c.published()
    assert e.value.code == "bad_hub_signature"


@pytest.mark.parametrize(
    "override",
    [
        ({"rows": [{**ROW, "content": "aGk"}]}, 200),  # an extra field: not metadata
        ({"rows": [{**ROW, "token_hash": "ab" * 32}]}, 200),  # a hash must never ride along
        ({"rows": [{**ROW, "has_password": "no"}]}, 200),  # a bool, not a record
        ({"rows": [{**ROW, "type": "release"}]}, 200),  # an unknown shape
        ({"rows": [{**ROW, "workspace_id": G.WS_C}]}, 200),  # another workspace than asked
        ({"rows": ROW}, 200),
        ({"rows": [], "x": 1}, 200),
        ([1], 200),
        ("not an object", 200),
    ],
)
def test_an_answer_that_breaks_the_contract_is_refused(fake, override):
    f = fake(st=state_of(), override=override)
    with pytest.raises(AdminReadError) as e:
        client(f.url).published(G.WS_A)
    assert e.value.code == "bad_answer"


@pytest.mark.parametrize(
    "override", [({"error": "boom"}, 500), ({"error": "malformed", "x": 1}, 400), ({}, 302), ({"error": "nope"}, 403)]
)
def test_other_answers_are_unavailable(fake, override):
    f = fake(st=state_of(), override=override)
    with pytest.raises(AdminReadError) as e:
        client(f.url).published()
    assert e.value.code == "unavailable"


def test_no_server_is_unavailable():
    with pytest.raises(AdminReadError) as e:
        client("http://127.0.0.1:1", timeout_s=1).published()
    assert e.value.code == "unavailable"


def test_a_non_loopback_http_url_is_refused():
    with pytest.raises(ValueError):
        client("http://example.com")


# --- the CLI --------------------------------------------------------------------------------------------------------


@pytest.fixture
def hub_state(tmp_path):
    st = tmp_path / "hub"
    keys = HubKeys.init(st)
    return st, keys


def test_cli_prints_the_rows(hub_state, fake, capsys):
    st, keys = hub_state
    # a fake publish that knows THIS hub's card and a full-mode binding of A to it
    root = keys.hk_root_pub
    b = G.signed(
        "wsk_a",
        "sig_hub_binding",
        {
            **G.hdr("hub.binding"),
            "workspace_id": G.WS_A,
            "wsk_pub": G.K.b("wsk_a"),
            "hub_id": keys.hub_id,
            "hk_root_pub": G.b64u(root),
            "mode": "full",
            "binding_seq": 1,
            "valid_from_ms": 0,
            "dev": True,
        },
    )
    now = keys._now()
    bs = H.apply_binding(
        None, H.verify_binding(b, workspace_id=G.WS_A, wsk_pub=G.K.pub["wsk_a"], now_ms=now, dev_mode=True)
    )
    f = fake(
        st={
            "cards": {G.WS_A: {"wsk_pub": G.K.b("wsk_a")}},
            "bindings": {G.WS_A: bs},
            "hub_cards": {keys.hub_id: keys.card()},
            "revoked_op_certs": [],
        },
        clock=lambda: now,
    )
    rc = cli.main(["releases", "--state", str(st), "--publish-url", f.url, "--json"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert json.loads(out.out) == {"published": [ROW]}


def test_cli_refuses_a_workspace_that_is_not_full_mode_bound(hub_state, fake, capsys):
    st, _ = hub_state
    Registry(str(st / "registry.db")).close()
    f = fake(st=state_of())
    rc = cli.main(["releases", "--state", str(st), "--publish-url", f.url, "--workspace", G.WS_A, "--json"])
    assert rc == 1 and "not_bound" in capsys.readouterr().err and f.seen == []


def test_cli_refuses_a_malformed_workspace_and_a_bad_url(hub_state, capsys):
    st, _ = hub_state
    rc = cli.main(
        ["releases", "--state", str(st), "--publish-url", "http://127.0.0.1:1", "--workspace", "XY", "--json"]
    )
    assert rc == 1 and "malformed" in capsys.readouterr().err
    rc = cli.main(["releases", "--state", str(st), "--publish-url", "http://example.com", "--json"])
    assert rc == 1
