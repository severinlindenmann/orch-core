"""HUB-SV-02/03 piece d: the console routes in front of the helper (admin only, nothing signed in the web layer)."""

from __future__ import annotations

import pytest
from webapi_helpers import Web

from orch_hub.contract.canon import b64u
from orch_hub.services import helper_backends as B
from orch_hub.services import helper_proto as P
from orch_hub.services.helper_proto import HelperError
from orch_hub.services.helper_server import HelperService
from orch_hub.services.helper_verify import ReadOnlyAuth

CFG = B.parse_config(
    {"v": 1, "host_id": "vps1", "parts": {"relay": {"label": "Relay", "kind": "systemd", "unit": "orch-relay.service"}}}
)


class InProcessClient:
    """The helper's rules behind the client's interface (the socket itself is tested in test_helper_server)."""

    def __init__(self, svc: HelperService) -> None:
        self.svc = svc
        self.calls: list[dict] = []

    def call(self, op, *, timeout_s=10, **fields):
        self.calls.append({"op": op, **fields})
        r = self.svc.handle({"op": op, **fields})
        if not r.pop("ok"):
            raise HelperError(r["code"], r.get("detail", ""))
        return r

    def info(self):
        return {"state": "up", **self.call("parts")}


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    state = tmp_path / "hub"
    x.ran = []
    x.nonces = P.NonceStore(state / "helper-nonce.db", now_ms=x.clock)
    x.svc = HelperService(
        state=state,
        config=CFG,
        auth=ReadOnlyAuth(state, now_ms=x.clock),
        nonces=x.nonces,
        runner=lambda argv, t: x.ran.append(list(argv)) or (0, "", ""),
        now_ms=x.clock,
        logs=lambda part: "09:00 relay ok",
    )
    x.api.d.helper = InProcessClient(x.svc)
    yield x
    x.nonces.close()
    x.close()


def _admin_session(w):
    tok = w.login(w.admin)
    signer, chan = w.register_channel(tok, w.admin)
    return tok, signer, chan


def test_prepare_sign_run_through_the_routes(w):
    tok, signer, chan = _admin_session(w)
    s, p, _ = w.call("POST", "/admin/services/relay/prepare", {"action": "restart", "channel_id": chan}, token=tok)
    assert s == 200 and p["ctx"]["user_id"] == w.admin and p["ctx"]["channel_id"] == chan
    assert p["digest"] == P.digest(p["ctx"]).hex()  # the console recomputes this from the shown context
    proof = b64u(signer.proof(bytes.fromhex(p["digest"])))
    s, r, _ = w.call(
        "POST", "/admin/services/relay/run", {"nonce": p["ctx"]["nonce"], "channel_id": chan, "proof": proof}, token=tok
    )
    assert (
        s == 200
        and r["accepted"] is True
        and w.ran == [["sudo", "-n", "/usr/bin/systemctl", "restart", "orch-relay.service"]]
    )
    s, o, _ = w.call(
        "POST", "/admin/services/relay/run", {"nonce": p["ctx"]["nonce"], "channel_id": chan, "proof": proof}, token=tok
    )
    assert (s, o["error"]) == (404, "unknown_prepared")


def test_non_admin_and_no_session_never_reach_the_helper(w):
    ann = w.login(w.ann)
    for path in ("/admin/services/relay/prepare", "/admin/services/relay/run"):
        assert w.call("POST", path, {"action": "stop", "channel_id": "0" * 32}, token=ann)[:2] == (
            403,
            {"error": "forbidden_role"},
        )
    assert w.call("GET", "/admin/services/relay/logs", token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.call("POST", "/admin/services/relay/prepare", {"action": "stop"})[0] in (401, 403)
    assert w.api.d.helper.calls == []


def test_csrf_and_bad_bodies(w):
    tok, _, chan = _admin_session(w)
    assert w.call(
        "POST", "/admin/services/relay/prepare", {"action": "stop", "channel_id": chan}, token=tok, csrf=False
    )[0] in (400, 403)
    assert w.call("POST", "/admin/services/relay/prepare", {"channel_id": chan}, token=tok)[1] == {"error": "malformed"}
    assert w.call("POST", "/admin/services/relay/prepare", {"action": 5, "channel_id": chan}, token=tok)[1] == {
        "error": "malformed"
    }
    s, o, _ = w.call("POST", "/admin/services/nope/prepare", {"action": "stop", "channel_id": chan}, token=tok)
    assert (s, o["error"]) == (404, "unknown_part")
    s, o, _ = w.call("POST", "/admin/services/relay/prepare", {"action": "update", "channel_id": chan}, token=tok)
    assert (s, o["error"]) == (409, "update_unavailable")


def test_logs_for_admin_only_text(w):
    tok, _, chan = _admin_session(w)
    s, o, _ = w.call("GET", f"/admin/services/relay/logs?channel_id={chan}", token=tok)
    assert s == 200 and o == {"part": "relay", "text": "09:00 relay ok"}


def test_services_page_carries_the_helper_state(w):
    from orch_hub.services import Services

    w.api.d.services = Services(relay_url=None, publish_url=None, probe=lambda *a: (200, None))
    tok = w.login(w.admin)
    o = w.call("GET", "/admin/services", token=tok)[1]
    assert o["helper"]["state"] == "up" and o["helper"]["parts"] == [{"name": "relay", "label": "Relay"}]
    w.api.d.helper = None
    assert w.call("GET", "/admin/services", token=tok)[1]["helper"] == {"state": "not_set_up", "parts": []}


def test_helper_down_is_a_plain_503(w):
    class Down:
        def call(self, *a, **k):
            raise HelperError("helper_unavailable")

        def info(self):
            return {"state": "down", "parts": []}

    w.api.d.helper = Down()
    tok, _, chan = _admin_session(w)
    s, o, _ = w.call("POST", "/admin/services/relay/prepare", {"action": "stop", "channel_id": chan}, token=tok)
    assert (s, o["error"]) == (503, "helper_unavailable")
