"""Helpers for the hub-removal tests (#1463): a recording launcher and a Web with a Closing service on real parts."""

from __future__ import annotations

from webapi_helpers import Web

from orch_hub.activity import ActivityLog
from orch_hub.closing import Closing
from orch_hub.contract import closing as C


class FakeLauncher:
    """Records what the hub would run. Never starts a process."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.dry = (True, "")
        self.fail_launch = False
        self.result: dict | None = None
        self.orders: list[dict] = []
        self.present: set[str] = set()

    def parts(self):
        return frozenset({"hub", "relay"})

    def write_order(self, state_dir, doc):
        self.calls.append(("write_order", doc["removal_id"]))
        self.orders.append(dict(doc))
        self.present.add(str(state_dir))

    def remove_order(self, state_dir):
        self.calls.append(("remove_order",))
        self.present.discard(str(state_dir))

    def read_result(self, state_dir):
        return self.result

    def dry_run(self, state_dir):
        self.calls.append(("dry_run", str(state_dir) in self.present))
        return self.dry

    def launch(self, state_dir, removal_id):
        self.calls.append(("launch", removal_id))
        if self.fail_launch:
            raise OSError("no runner")


def make_web(tmp_path, *, inline=True):
    """A Web with a Closing service. ``inline``: the worker runs inside the confirm request (else ``w.spawned`` collects it)."""
    w = Web(tmp_path)
    w.state = tmp_path / "hub"
    act = w.api.d.activity = ActivityLog(":memory:")
    w.launcher = FakeLauncher()
    w.spawned = []
    w.closing = w.api.d.closing = Closing(
        keys=w.env.keys,
        access=w.env.acc,
        registry=w.reg,
        channels=w.env.svc,
        now_ms=w.clock,
        launcher=w.launcher,
        activity=act,
        audit=w.env.audit,
        spawn=(lambda fn: fn()) if inline else w.spawned.append,
    )
    w.pushed = []
    w.closing.push = lambda ws, note: (w.pushed.append((ws, note)), True)[1]
    w._act = act
    return w


def with_backup_key(w):
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/backup/key", w.creds(w.admin), token=tok)
    assert s == 200, o
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[0] == 200
    return o["words"]


def admin_with_key(w):
    """The admin signed in with a registered browser channel key. Returns (token, signer, channel_id)."""
    tok = w.login(w.admin)
    signer, ch = w.register_channel(tok, w.admin)
    return tok, signer, ch


def prepare(w, tok, ch):
    return w.call("POST", "/admin/hub/remove/prepare", {"channel_id": ch}, token=tok)


def confirm_body(w, signer, prep, **over):
    # the page recomputes the digest from ctx itself and signs that, never the digest the server sent
    local = C.remove_digest(prep["ctx"]).hex()
    assert local == prep["digest"]
    return {"nonce": prep["nonce"], "proof": w.proof(signer, local), **w.creds(w.admin), **over}


def start(w, tok, signer, ch):
    s, prep, _ = prepare(w, tok, ch)
    assert s == 200, prep
    return w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=tok)


def close_all(w):
    w._act.close()
    w.close()
