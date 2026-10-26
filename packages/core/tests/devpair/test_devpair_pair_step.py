"""``step_pair`` against a stand-in for ``orch host relay pair`` (a child that talks like it: JSON state lines on
``--json-fd``, the SAS prompt on its terminal) with a fake control socket and a fake owner: the link goes to
``pair.json`` (0600) and the ``--json-fd`` pipe and never to stdout; the SAS is typed only from a FIFO line for this
offer and phase; the owner serves the cert request when the offer reaches ``cert_pending`` and the certificate is
handed to the host."""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from types import SimpleNamespace

OFFER = "ef" * 16
LINK = "https://relay.example/pair#v2." + "x" * 80
NATIVE = "orch://pair?relay=x#v2." + "y" * 80

CHILD = r"""
import json, os, sys
fd = int(sys.argv[sys.argv.index('--json-fd') + 1])
def emit(o): os.write(fd, json.dumps(o).encode() + b'\n')
emit({"offer_id": "%(offer)s", "state": "open", "link": "%(link)s", "native_link": "%(native)s", "expires_ms": 123})
emit({"offer_id": "%(offer)s", "state": "pending", "expires_ms": 123})
tty = os.open('/dev/tty', os.O_RDWR)
os.write(tty, b'request from: [phone]\ntype the 6 characters the phone shows (empty: reject): ')
line = b''
while not line.endswith(b'\n'):
    line += os.read(tty, 1)
open(os.environ['ORCH_TEST_SAS_OUT'], 'w').write(line.decode().strip())
emit({"offer_id": "%(offer)s", "state": "cert_pending", "expires_ms": 123})
import time
for _ in range(20):
    if os.path.exists(os.environ['ORCH_TEST_CERT_FLAG']):
        break
    time.sleep(0.1)
else:
    raise SystemExit(4)
emit({"offer_id": "%(offer)s", "state": "approved", "expires_ms": 123})
"""
for _k, _v in (("%(offer)s", OFFER), ("%(link)s", LINK), ("%(native)s", NATIVE)):
    CHILD = CHILD.replace(_k, _v)


class FakeOwner:
    def __init__(self, flag, code):
        self.flag, self.code, self.served = flag, code, []

    def serve_request(self, request_id, workspace_id, state_dir, read_code, *, wait_s):
        self.served.append(request_id)
        typed = read_code("phone", "look,decide")
        cert = {"o": {"device_id": "d" * 32}, "typed": typed}
        if typed == self.code:
            open(self.flag, "w").close()
        return SimpleNamespace(cert=cert if typed == self.code else None, rejected=typed != self.code)

    def close(self):
        pass


class FakeCall:
    def __init__(self):
        self.calls = []

    def __call__(self, op, args):
        self.calls.append((op, args))
        if op == "host.relay_pair_list":
            return {"offers": [{"offer_id": OFFER, "outstanding_request": "aa" * 16}]}
        return {"state": "ok"}

    def close(self):
        pass


def setup(dp, scratch, monkeypatch, *, with_fifo=True, code="K7M2QX"):
    monkeypatch.setattr(dp, "sticky_problem", lambda p: None)
    root = dp.check_root_path(str(scratch / "sb"))
    sb = dp.create_root(root)
    for n in ("ws", "state", "run"):
        sb.mkdir(n)
    dp.manifest_update(root, entries=["ws", "state", "run", "pair.json", "codes"])
    script = scratch / "child.py"
    script.write_text(CHILD)
    args = Namespace(scopes="look,decide", json_fd=None, open_simulator=False)
    ctx = dp.Ctx(sb=sb, args=args, path_env="/usr/bin:/bin", py=sys.executable, uv="/usr/bin/true")
    ctx.ws_dir, ctx.workspace_id, ctx.origin = str(root / "ws"), "ab" * 16, "http://127.0.0.1:1"
    ctx.passphrase = "unused words"
    flag = scratch / "cert-flag"
    sas_out = scratch / "sas-out"
    ctx.orch = lambda *a: [sys.executable, str(script), *a]  # type: ignore[method-assign]
    ctx.env = lambda **kw: {**os.environ, "ORCH_TEST_SAS_OUT": str(sas_out), "ORCH_TEST_CERT_FLAG": str(flag)}  # type: ignore[method-assign]
    owner = FakeOwner(flag, code)
    call = FakeCall()
    monkeypatch.setattr(dp, "make_owner", lambda c: owner)
    monkeypatch.setattr(dp, "ControlCall", lambda r: call)
    fifo = None
    if with_fifo:
        os.mkfifo(root / "codes", 0o600)
        ctx.fifo = fifo = dp.CodeFifo(root / "codes", root)
    return ctx, owner, call, sas_out, fifo


def test_pair_step_types_the_fifo_code_and_serves_the_primary(dp, scratch, monkeypatch, capsys):
    ctx, owner, call, sas_out, fifo = setup(dp, scratch, monkeypatch)
    r, w = os.pipe()
    ctx.args.json_fd = w
    wfd = os.open(ctx.root / "codes", os.O_WRONLY | os.O_NONBLOCK)
    for line in ("cd" * 16 + " sas ZZZZZZ", OFFER + " sas SASSAS", OFFER + " cert_code K7M2QX"):
        os.write(wfd, line.encode() + b"\n")  # a stale one (other offer), the SAS, the cert_code
    try:
        assert dp.step_pair(ctx) == 0
    finally:
        os.close(wfd)
        fifo.close()
    assert sas_out.read_text() == "SASSAS"  # the stale line was never typed
    assert owner.served == ["aa" * 16]
    cert = {"o": {"device_id": "d" * 32}, "typed": "K7M2QX"}
    assert ("host.relay_pair_cert", {"workspace_id": ctx.workspace_id, "offer_id": OFFER, "cert": cert}) in call.calls
    pair = json.loads((ctx.root / "pair.json").read_text())
    assert pair == {
        "link": LINK, "native_link": NATIVE, "offer_id": OFFER, "expires_ms": 123, "origin": "http://127.0.0.1:1",
        "workspace_id": ctx.workspace_id, "root": str(ctx.root),
    }  # fmt: skip
    assert (os.stat(ctx.root / "pair.json").st_mode & 0o777) == 0o600
    assert json.loads(os.read(r, 4096).splitlines()[0]) == pair  # the --json-fd pipe got it too
    out = capsys.readouterr()
    assert LINK not in out.out + out.err and NATIVE not in out.out + out.err and "pair.json" in out.out
    os.close(r)
    os.close(w)


def test_pair_step_rejects_when_the_compare_code_is_wrong(dp, scratch, monkeypatch):
    ctx, owner, call, sas_out, fifo = setup(dp, scratch, monkeypatch)
    wfd = os.open(ctx.root / "codes", os.O_WRONLY | os.O_NONBLOCK)
    os.write(wfd, f"{OFFER} sas SASSAS\n{OFFER} cert_code WRONG1\n".encode())
    try:
        assert dp.step_pair(ctx) == 1  # the stand-in waits for the certificate and gives up (exit 4)
    finally:
        os.close(wfd)
        fifo.close()
    assert [c[0] for c in call.calls if c[0] != "host.relay_pair_list"] == ["host.relay_pair_cancel"]


def test_pair_step_without_a_code_rejects_the_sas(dp, scratch, monkeypatch):
    ctx, owner, call, sas_out, fifo = setup(dp, scratch, monkeypatch)
    monkeypatch.setattr(dp, "SAS_WAIT_S", 0.5)  # nobody types anything
    try:
        dp.step_pair(ctx)
    finally:
        fifo.close()
    assert sas_out.read_text() == ""  # an empty line is the CLI's "reject"


def test_a_failing_owner_cancels_the_offer(dp, scratch, monkeypatch):
    ctx, owner, call, sas_out, fifo = setup(dp, scratch, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("relay down")

    owner.serve_request = boom
    wfd = os.open(ctx.root / "codes", os.O_WRONLY | os.O_NONBLOCK)
    os.write(wfd, f"{OFFER} sas SASSAS\n".encode())
    try:
        assert dp.step_pair(ctx) == 1
    finally:
        os.close(wfd)
        fifo.close()
    assert [c[0] for c in call.calls if c[0] != "host.relay_pair_list"] == ["host.relay_pair_cancel"]
