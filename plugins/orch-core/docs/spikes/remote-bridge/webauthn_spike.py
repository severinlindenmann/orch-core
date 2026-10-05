"""R0 spike (throwaway). Question 4: a host-issued challenge answered by a platform authenticator in a browser and
verified on the host. Chromium's virtual authenticator stands in for Face ID / Touch ID; a real device is a manual check.

Run with the core venv:  .venv/bin/python docs/spikes/remote-bridge/webauthn_spike.py
localhost is a secure context, so no TLS here; a real TIX origin is https (rp id = its host name).
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import time

import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright

b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
unb64u = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def cbor(buf, i=0):
    """Minimal CBOR reader: ints, bytes, text, arrays, maps (all a COSE key and an attestation object need)."""
    ib = buf[i]; mt, ai = ib >> 5, ib & 31; i += 1
    if ai < 24: n = ai
    elif ai == 24: n = buf[i]; i += 1
    elif ai == 25: n = struct.unpack(">H", buf[i:i + 2])[0]; i += 2
    elif ai == 26: n = struct.unpack(">I", buf[i:i + 4])[0]; i += 4
    else: raise ValueError("cbor length")
    if mt == 0: return n, i
    if mt == 1: return -1 - n, i
    if mt == 2: return buf[i:i + n], i + n
    if mt == 3: return buf[i:i + n].decode(), i + n
    if mt == 4:
        a = []
        for _ in range(n): v, i = cbor(buf, i); a.append(v)
        return a, i
    if mt == 5:
        d = {}
        for _ in range(n): k, i = cbor(buf, i); v, i = cbor(buf, i); d[k] = v
        return d, i
    raise ValueError("cbor type")


class Host:
    """The host side: issues a challenge bound to the exact action text, verifies the assertion."""

    def __init__(self, origin, rp_id):
        self.origin, self.rp_id = origin, rp_id
        self.creds = {}         # credential id -> (public key, sign count)
        self.challenges = {}    # challenge -> (action text, issued at)

    def issue(self, action: str):
        ch = secrets.token_bytes(32)
        # the challenge covers the exact thing the person is shown
        bound = hashlib.sha256(ch + action.encode()).digest()
        self.challenges[b64u(bound)] = (action, time.time())
        return b64u(bound)

    def register(self, att_b64, client_b64):
        client = json.loads(unb64u(client_b64))
        assert client["type"] == "webauthn.create" and client["origin"] == self.origin
        att, _ = cbor(unb64u(att_b64))
        ad = att["authData"]
        assert ad[:32] == hashlib.sha256(self.rp_id.encode()).digest()
        flags = ad[32]
        assert flags & 0x40, "no attested credential data"
        idlen = struct.unpack(">H", ad[53:55])[0]
        cid = ad[55:55 + idlen]
        key, _ = cbor(ad, 55 + idlen)
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(key[-2], "big"), int.from_bytes(key[-3], "big"), ec.SECP256R1()).public_key()
        self.creds[cid] = [pub, 0]
        return b64u(cid)

    def verify(self, cid_b64, ad_b64, client_b64, sig_b64):
        """Returns (ok, reason)."""
        try:
            client_raw = unb64u(client_b64)
            client = json.loads(client_raw)
            if client.get("type") != "webauthn.get": return False, "type"
            if client.get("origin") != self.origin: return False, "origin"
            issued = self.challenges.pop(client.get("challenge"), None)   # single use
            if issued is None: return False, "unknown or replayed challenge"
            if time.time() - issued[1] > 60: return False, "expired"
            ad = unb64u(ad_b64)
            if ad[:32] != hashlib.sha256(self.rp_id.encode()).digest(): return False, "rpIdHash"
            flags = ad[32]
            if not flags & 0x01: return False, "user not present"
            if not flags & 0x04: return False, "user not verified (UV flag clear)"
            cred = self.creds.get(unb64u(cid_b64))
            if cred is None: return False, "unknown credential"
            count = struct.unpack(">I", ad[33:37])[0]
            if count and count <= cred[1]: return False, "sign count did not increase"
            cred[0].verify(unb64u(sig_b64), ad + hashlib.sha256(client_raw).digest(), ec.ECDSA(hashes.SHA256()))
            cred[1] = count
            return True, issued[0]
        except Exception as e:  # InvalidSignature and parse errors alike: fail closed
            return False, "invalid: " + type(e).__name__


PAGE = """<!doctype html><meta charset=utf-8><title>tix-like</title><body>
<script>
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
const unb=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-s.length%4)%4)),c=>c.charCodeAt(0));
const post=(p,o)=>fetch(p,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(o)}).then(r=>r.json());
window.register=async()=>{
  const c=await fetch('/reg-challenge').then(r=>r.json());
  const cred=await navigator.credentials.create({publicKey:{challenge:unb(c.challenge),rp:{name:'TIX spike',id:location.hostname},
    user:{id:new TextEncoder().encode('owner'),name:'owner',displayName:'owner'},pubKeyCredParams:[{type:'public-key',alg:-7}],
    authenticatorSelection:{authenticatorAttachment:'platform',userVerification:'required',residentKey:'preferred'},attestation:'none',timeout:20000}});
  return post('/register',{att:b64u(cred.response.attestationObject),client:b64u(cred.response.clientDataJSON)});
};
window.assertion=async(action,tamper)=>{
  const c=await fetch('/challenge?action='+encodeURIComponent(action)).then(r=>r.json());
  const t0=performance.now();
  const a=await navigator.credentials.get({publicKey:{challenge:unb(c.challenge),rpId:location.hostname,userVerification:'required',timeout:20000}});
  const ms=performance.now()-t0;
  let client=b64u(a.response.clientDataJSON);
  if(tamper==='client'){const j=JSON.parse(new TextDecoder().decode(unb(client)));j.origin='https://evil.example';client=b64u(new TextEncoder().encode(JSON.stringify(j)));}
  const r=await post('/verify',{id:b64u(a.rawId),ad:b64u(a.response.authenticatorData),client,sig:b64u(a.response.signature)});
  r.get_ms=Math.round(ms); r.rawChallengeOk=c.challenge; return r;
};
window.replay=async(action)=>{ // answer once, then submit the same answer again
  const c=await fetch('/challenge?action='+encodeURIComponent(action)).then(r=>r.json());
  const a=await navigator.credentials.get({publicKey:{challenge:unb(c.challenge),rpId:location.hostname,userVerification:'required',timeout:20000}});
  const body={id:b64u(a.rawId),ad:b64u(a.response.authenticatorData),client:b64u(a.response.clientDataJSON),sig:b64u(a.response.signature)};
  return [await post('/verify',body), await post('/verify',body)];
};
window.inFrame=()=>new Promise(res=>{const f=document.createElement('iframe');f.setAttribute('sandbox','allow-scripts');
  f.srcdoc='<script>navigator.credentials.get({publicKey:{challenge:new Uint8Array(32),userVerification:"required",timeout:3000}}).then(()=>parent.postMessage("ok","*"),e=>parent.postMessage(e.name+": "+e.message,"*"))<\\/script>';
  addEventListener('message',e=>res(String(e.data)));document.body.append(f);});
</script>"""


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def main():
    port = free_port()
    origin = f"http://localhost:{port}"
    host = Host(origin, "localhost")
    app = FastAPI()

    @app.get("/")
    async def index(): return HTMLResponse(PAGE)

    @app.get("/reg-challenge")
    async def regc(): return {"challenge": b64u(secrets.token_bytes(32))}

    @app.get("/challenge")
    async def chal(action: str): return {"challenge": host.issue(action)}

    @app.post("/register")
    async def reg(request: Request):
        b = await request.json(); return {"credential": host.register(b["att"], b["client"])}

    @app.post("/verify")
    async def ver(request: Request):
        b = await request.json(); ok, why = host.verify(b["id"], b["ad"], b["client"], b["sig"]); return {"ok": ok, "why": why}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    srv.install_signal_handlers = lambda: None
    task = asyncio.create_task(srv.serve())
    while not srv.started: await asyncio.sleep(0.05)
    res = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for uv in (True, False):
            ctx = await b.new_context()
            page = await ctx.new_page()
            await page.goto(origin + "/")
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("WebAuthn.enable")
            await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
                "isUserVerified": uv, "automaticPresenceSimulation": True}})
            r = {}
            if uv:
                r["register"] = await page.evaluate("register()")
                r["assert_ok"] = await page.evaluate("assertion('Start epic E-12: Migrate jobs')")
                r["assert_tampered_client"] = await page.evaluate("assertion('Start epic E-12', 'client')")
                r["replay"] = await page.evaluate("replay('Allow permission X')")
                r["in_sandboxed_frame"] = await page.evaluate("inFrame()")
            else:
                # an authenticator that cannot verify the user: with userVerification required the browser/authenticator refuses,
                # or the host sees the UV flag clear
                try:
                    r["register_no_uv"] = await page.evaluate("register()")
                except Exception as e:
                    r["register_no_uv"] = "browser refused: " + str(e)[:120]
            res["uv_true" if uv else "uv_false"] = r
            await ctx.close()
        await b.close()
    srv.should_exit = True
    await task
    print(json.dumps(res, indent=1))
    json.dump(res, open(os.path.join(os.path.dirname(__file__), "webauthn-results.json"), "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())
