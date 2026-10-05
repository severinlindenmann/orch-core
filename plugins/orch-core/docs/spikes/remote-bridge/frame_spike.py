"""R0 spike (throwaway). Question 3: the real dashboard app inside an opaque-origin sandboxed frame behind a shim.

Run with the core venv from plugins/orch-core:  .venv/bin/python docs/spikes/remote-bridge/frame_spike.py
Only a TEMPORARY fixture workspace is used; ORCH_STATE_DIR / XDG_CONFIG_HOME / CLAUDE_CONFIG_DIR point into it.
Localhost only. The dashboard's human-only actions refuse for an agent process: expected, not worked around.
"""
import asyncio
import base64
import json
import os
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="r0-frame-"))
# isolate BEFORE importing orch
os.environ["ORCH_STATE_DIR"] = str(TMP / "state")
os.environ["XDG_CONFIG_HOME"] = str(TMP / "xdg")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude")
os.environ.pop("ORCH_HOME", None)
for d in ("state", "xdg", "claude"):
    (TMP / d).mkdir()
sys.path.insert(0, str(HERE.parents[2] / "src"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402

TOKEN = "spike-token"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def build_workspace():
    from orch.clock import stamp
    from orch.config.load import deep_merge
    from orch.core import store
    from orch.core.ids import next_id
    from orch.core.model import new_ticket
    from orch.core.workspace import Workspace
    root = TMP / "workspace"
    home = root / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps(deep_merge({"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4}}, {})), encoding="utf-8")
    ws = Workspace.open(root)
    for title, status in [("Migrate the batch jobs", "open"), ("Rotate the signing key", "in-progress"), ("Write the runbook", "backlog"), ("Check the nightly backup", "testing")]:
        t = new_ticket(next_id(ws), title, type="feature", priority="normal", size="m", created=stamp())
        t.meta["status"] = status
        t.set_section("Ask", "Spike fixture ticket.")
        store.save(ws, t)
    return ws


def add_ticket(ws, title):
    from orch.clock import stamp
    from orch.core import store
    from orch.core.ids import next_id
    from orch.core.model import new_ticket
    t = new_ticket(next_id(ws), title, type="feature", priority="normal", size="m", created=stamp())
    t.meta["status"] = "backlog"
    store.save(ws, t)
    return t.id


SHELL = """<!doctype html><meta charset=utf-8><title>shell</title>
<body style="margin:0;font:14px system-ui"><div style="height:40px"><button id=back>Back</button> TIX-origin shell (spike)</div>
<script src="/shell.js"></script>"""

PROBE_SHELL = """<!doctype html><meta charset=utf-8><title>probe</title><body>
<script>window.__probe=null;addEventListener('message',e=>{if(e.data&&e.data.k==='probe')window.__probe=e.data.r});</script>
<iframe name=probe sandbox="allow-scripts allow-forms" src="/probe-frame" style="width:600px;height:300px"></iframe>"""

PROBE_JS = r"""(async()=>{
const r={},v=[];
document.addEventListener('securitypolicyviolation',e=>v.push(e.violatedDirective+' '+(e.blockedURI||'').slice(0,40)));
const t=async(n,f)=>{try{const x=await f();r[n]='ok'+(x===undefined?'':': '+String(x).slice(0,80))}catch(e){r[n]='throws '+e.name+': '+String(e.message).slice(0,70)}};
await t('document.cookie read',()=>document.cookie);
await t('document.cookie write',()=>{document.cookie='a=b';return 'no throw'});
await t('localStorage',()=>localStorage.getItem('x'));
await t('sessionStorage',()=>sessionStorage.getItem('x'));
await t('indexedDB',()=>{indexedDB.open('x');return 'opened'});
await t('location.href/origin/pathname',()=>location.href+' | '+location.origin+' | '+location.pathname);
await t('history.replaceState other path',()=>{history.replaceState(null,'','/board?x=1');return location.pathname+location.search});
await t('history.pushState other path',()=>{const n=history.length;history.pushState(null,'','/t/L-0001');return 'len '+n+'->'+history.length+' '+location.pathname});
await t('replaceState other origin',()=>{history.replaceState(null,'','http://example.com/x')});
await t('fetch same origin',async()=>{const x=await fetch('/healthz');return x.status});
await t('XMLHttpRequest',()=>new Promise((res,rej)=>{const x=new XMLHttpRequest();x.open('GET','/healthz');x.onload=()=>res('status '+x.status);x.onerror=()=>rej(new Error('network error'));x.send()}));
await t('EventSource',()=>new Promise((res,rej)=>{const e=new EventSource('/events');e.onopen=()=>res('open');e.onerror=()=>rej(new Error('error event'));setTimeout(()=>rej(new Error('timeout')),1500)}));
await t('window.open',()=>String(window.open('about:blank')));
await t('navigator.clipboard.writeText',async()=>{if(!navigator.clipboard)return 'navigator.clipboard undefined';await navigator.clipboard.writeText('x');return 'written'});
await t('document.execCommand copy',()=>document.execCommand('copy'));
await t('Worker(blob:)',()=>{const w=new Worker(URL.createObjectURL(new Blob(['1'])));w.terminate();return 'started'});
await t('BroadcastChannel',()=>{new BroadcastChannel('x').close();return 'ok'});
await t('serviceWorker',()=>navigator.serviceWorker?'present':'undefined');
await t('document.referrer',()=>JSON.stringify(document.referrer));
await t('window.top.location',()=>window.top.location.href);
await t('parent.postMessage',()=>{parent.postMessage({k:'probe-ping'},location.origin);return 'sent'});
await t('dialog.showModal',()=>{const d=document.createElement('dialog');document.body.append(d);d.showModal();const o=d.open;d.close();return o});
await t('matchMedia',()=>matchMedia('(max-width:900px)').matches);
await t('img blob: load',()=>new Promise((res,rej)=>{const i=new Image();i.onload=()=>res('loaded');i.onerror=()=>rej(new Error('blocked'));i.src=URL.createObjectURL(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'],{type:'image/svg+xml'}))}));
await t('img same-origin /static load',()=>new Promise((res,rej)=>{const i=new Image();i.onload=()=>res('loaded');i.onerror=()=>rej(new Error('blocked'));i.src='/static/x.png';setTimeout(()=>rej(new Error('timeout')),800)}));
await t('script src same-origin',()=>new Promise((res,rej)=>{const s=document.createElement('script');s.src='/shell.js';s.onload=()=>res('loaded');s.onerror=()=>rej(new Error('blocked'));document.head.append(s);setTimeout(()=>rej(new Error('timeout')),800)}));
await t('inline script w/o nonce (eval)',()=>{try{return eval('1+1')}catch(e){throw e}});
await t('form submit (form-action none)',()=>{const f=document.createElement('form');f.action='/probe-form';f.method='post';document.body.append(f);f.submit();return 'submitted'});
await new Promise(r=>setTimeout(r,500));
r.cspViolations=v.join(' || ');
parent.postMessage({k:'probe',r},location.origin);
})();"""


def frame_csp(nonce):
    return (f"sandbox allow-scripts allow-forms; default-src 'none'; script-src 'nonce-{nonce}'; img-src blob: data:; "
            "font-src blob:; style-src 'unsafe-inline'; connect-src 'none'; frame-src 'none'; form-action 'none'; "
            "base-uri 'none'; frame-ancestors 'self'")


def make_shell_app(dash_base, ws):
    app = FastAPI()
    stats = {"relayed": 0, "relay_server_ms": [], "paths": [], "navs_to_frame_route": []}
    app.state.stats = stats
    app.state.keys = []
    client = httpx.AsyncClient(base_url=dash_base, timeout=30, follow_redirects=True)
    app.state.client = client
    shim = (HERE / "shim.js").read_text()

    @app.on_event("startup")
    async def login():
        r = await client.get("/?token=" + TOKEN)   # the host's own cookie, never sent to the browser
        assert r.status_code == 200, r.status_code

    @app.get("/shell", response_class=HTMLResponse)
    async def shell():
        return HTMLResponse(SHELL, headers={"Content-Security-Policy": "default-src 'none'; script-src 'self'; frame-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'"})

    @app.get("/shell.js")
    async def shelljs():
        return Response((HERE / "shell.js").read_text(), media_type="text/javascript")

    @app.get("/frame")
    async def frame(tok: str = ""):
        stats["navs_to_frame_route"].append(time.time())
        nonce = secrets.token_urlsafe(18)
        html = f'<!doctype html><meta charset=utf-8><title>frame</title><body><script nonce="{nonce}" data-tok="{tok}">{shim}</script>'
        return HTMLResponse(html, headers={"Content-Security-Policy": frame_csp(nonce), "Cache-Control": "no-store"})

    @app.get("/probe-shell")
    async def probe_shell():
        return HTMLResponse(PROBE_SHELL, headers={"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; frame-src 'self'"})

    @app.get("/probe-frame")
    async def probe_frame():
        nonce = secrets.token_urlsafe(18)
        return HTMLResponse(f'<!doctype html><meta charset=utf-8><body><script nonce="{nonce}">{PROBE_JS}</script>',
                            headers={"Content-Security-Policy": frame_csp(nonce)})

    @app.post("/relay")
    async def relay(request: Request):
        from urllib.parse import unquote
        t0 = time.perf_counter()
        meta = json.loads(unquote(request.headers["x-spike-req"]))
        body = await request.body()
        fwd = {k: v for k, v in meta["headers"].items() if k in ("content-type", "accept")}
        if meta["method"] not in ("GET", "HEAD"):
            fwd["origin"] = str(client.base_url).rstrip("/")   # the host sets its own Origin, consistent with its own Host
        r = await client.request(meta["method"], meta["path"], content=body or None, headers=fwd)
        if meta["path"].endswith("/keys"):
            app.state.keys.append(meta["path"])
        keep = {k: v for k, v in r.headers.items() if k in ("content-type", "content-security-policy", "content-disposition", "cache-control", "etag")}
        final = r.url.raw_path.decode()
        out = {"status": r.status_code, "headers": keep, "url": final}
        stats["relayed"] += 1
        stats["paths"].append(f'{meta["method"]} {meta["path"]} -> {r.status_code}')
        stats["relay_server_ms"].append((time.perf_counter() - t0) * 1000)
        from urllib.parse import quote
        return Response(r.content, status_code=200, headers={"x-spike-res": quote(json.dumps(out)), "content-type": "application/octet-stream"})

    @app.get("/relay-stream")
    async def relay_stream(path: str):
        async def gen():
            async with client.stream("GET", path, timeout=None, headers={"accept-encoding": "identity"}) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/stats")
    async def get_stats():
        return stats

    return app


async def serve(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    srv.install_signal_handlers = lambda: None
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    return srv, task


def fake_terminals(ws):
    """Terminals needs the addon enabled and tmux sessions on the orch tmux server. Both are faked in THIS process
    only, so the page, its server-sent stream and the key POSTs run without touching a real tmux server."""
    from orch.dashboard import terminals as T
    T.addon_on = lambda w: True
    T.available = lambda: True
    sess = T.Session("orch-spike-1", str(ws.root), int(time.time()), int(time.time()), 100, 30, 0)
    T.sessions = lambda w: [sess]

    def cap(name):
        if name != sess.name:
            return None
        n = int(time.time() * 4)
        h = f"t={n / 4:.2f}\n" + "\n".join(f"line {i} " + "x" * 60 for i in range(40))
        return {"html": h, "trim": h, "tail": h[:600], "cols": 100, "rows": 30}
    T.capture = cap
    T.capture_many = lambda names: {n: cap(n) for n in names}
    T.send = lambda name, seq: None
    T.resize = lambda *a: None


async def main():
    from orch.dashboard.app import create_app
    ws = build_workspace()
    fake_terminals(ws)
    dash_port, shell_port = free_port(), free_port()
    dash = create_app(ws, TOKEN)   # port=None: no switcher registration
    dsrv, dtask = await serve(dash, dash_port)
    shell = make_shell_app(f"http://127.0.0.1:{dash_port}", ws)
    ssrv, stask = await serve(shell, shell_port)
    print("workspace", TMP, "dash", dash_port, "shell", shell_port, flush=True)
    import drive
    try:
        await drive.run(f"http://127.0.0.1:{shell_port}", shell, ws, add_ticket, HERE)
    finally:
        ssrv.should_exit = True; dsrv.should_exit = True
        await asyncio.gather(stask, dtask, return_exceptions=True)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    asyncio.run(main())
