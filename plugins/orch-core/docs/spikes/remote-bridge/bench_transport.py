"""R0 spike (throwaway). Questions 1, 2, 5: mailbox latency and commit time on a local TIX.

Run with the TIX worktree's venv from the TIX worktree root:
  .venv/bin/python <this file> [--n 200] [--stream-s 30]
AES-GCM stands in for sealing. Localhost only; temp data dir; starts and stops its own uvicorn.
"""
import argparse
import asyncio
import json
import os
import random
import socket
import statistics as st
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PAGE_BYTES = 60_000
KEY = AESGCM.generate_key(bit_length=256)
AES = AESGCM(KEY)


def seal(b: bytes) -> bytes:
    n = os.urandom(12)
    return n + AES.encrypt(n, b, None)


def unseal(b: bytes) -> bytes:
    return AES.decrypt(b[:12], b[12:], None)


def unpack(buf: bytes):
    out, i = [], 0
    while i < len(buf):
        l = int.from_bytes(buf[i:i + 4], "big"); i += 4
        rid = buf[i:i + l].decode(); i += l
        idx = int.from_bytes(buf[i:i + 4], "big"); i += 4
        last = buf[i]; i += 1
        bl = int.from_bytes(buf[i:i + 4], "big"); i += 4
        out.append((rid, idx, last, buf[i:i + bl])); i += bl
    return out


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def summ(xs):
    return {"n": len(xs), "p50": round(pct(xs, .5)), "p95": round(pct(xs, .95)), "max": round(max(xs)),
            "mean": round(st.mean(xs))}  # ms


async def host_loop(base, mb, stop, handler):
    async with httpx.AsyncClient(base_url=base, timeout=40) as c:
        while not stop.is_set():
            try:
                r = await c.get(f"/spike/mb/{mb}/req", params={"wait": 20})
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            for rid, _, _, body in unpack(r.content):
                asyncio.create_task(handler(c, mb, rid, unseal(body)))


async def page_handler(c, mb, rid, req):
    page = os.urandom(PAGE_BYTES)
    chunk = 15_000
    for i in range(0, len(page), chunk):
        last = int(i + chunk >= len(page))
        await c.post(f"/spike/mb/{mb}/resp/{rid}", params={"idx": i // chunk, "last": last}, content=seal(page[i:i + chunk]))


async def stream_handler(c, mb, rid, req):
    cfg = json.loads(req)
    period = 1 / cfg["fps"]
    t_next = time.monotonic()
    for i in range(int(cfg["seconds"] * cfg["fps"])):
        t_next += period
        body = json.dumps({"t": time.time()}).encode().ljust(cfg["size"], b" ")
        await c.post(f"/spike/mb/{mb}/resp/{rid}", params={"idx": i, "last": int(i == int(cfg["seconds"] * cfg["fps"]) - 1)}, content=seal(body))
        await asyncio.sleep(max(0, t_next - time.monotonic()))


async def device_request(c, mb, payload: bytes, collect):
    """POST sealed request, long-poll the response; collect(chunks, t_recv) is called per delivery; returns when last seen."""
    rid = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    await c.post(f"/spike/mb/{mb}/req/{rid}", content=seal(payload))
    done = False
    while not done:
        r = await c.get(f"/spike/mb/{mb}/resp/{rid}", params={"wait": 20})
        if r.status_code != 200:
            continue
        now = time.time()
        items = unpack(r.content)
        collect([(i, last, unseal(b)) for _, i, last, b in items], now)
        done = any(l for _, _, l, _ in items)
    return (time.perf_counter() - t0) * 1000


async def bench_page(base, n):
    mb, stop = "m-" + uuid.uuid4().hex[:6], asyncio.Event()
    h = asyncio.create_task(host_loop(base, mb, stop, page_handler))
    await asyncio.sleep(0.3)
    rt = []
    async with httpx.AsyncClient(base_url=base, timeout=40) as c:
        for _ in range(n):
            await asyncio.sleep(random.random() * 0.5)  # de-phase from the 0.5 s step
            rt.append(await device_request(c, mb, b"GET /tickets", lambda a, b: None))
    stop.set(); h.cancel()
    return rt


async def bench_stream(base, secs, viewers=1, fps=4, size=4096, ramp_hosts=None):
    """viewers streams, each its own mailbox/host, 4 fps x 4 KB. Returns frame latencies in ms."""
    stop, lat = asyncio.Event(), []
    hosts, devs = [], []
    mbs = ["s-" + uuid.uuid4().hex[:6] for _ in range(viewers)]
    for mb in mbs:
        hosts.append(asyncio.create_task(host_loop(base, mb, stop, stream_handler)))
    await asyncio.sleep(0.3)

    def collect(chunks, now):
        for _, _, body in chunks:
            lat.append((now - json.loads(body)["t"]) * 1000)

    async def viewer(mb):
        async with httpx.AsyncClient(base_url=base, timeout=40) as c:
            await device_request(c, mb, json.dumps({"fps": fps, "seconds": secs, "size": size}).encode(), collect)

    async def heartbeats(mb):
        async with httpx.AsyncClient(base_url=base, timeout=20) as c:
            while not stop.is_set():
                await c.post(f"/spike/mb/{mb}/presence", content=seal(b"p" * 200))
                await asyncio.sleep(10)

    hb = [asyncio.create_task(heartbeats("hb-%d" % i)) for i in range(3)]
    await asyncio.gather(*(viewer(mb) for mb in mbs))
    stop.set()
    for t in hosts + hb:
        t.cancel()
    return lat


async def main(a):
    global PAGE_BYTES
    PAGE_BYTES = a.page_kb * 1000
    port = socket.socket(); port.bind(("127.0.0.1", 0)); p = port.getsockname()[1]; port.close()
    data = tempfile.mkdtemp(prefix="r0-tix-")
    env = {**os.environ, "FS_DATA_DIR": data, "FS_COOKIE_SECURE": "0"}
    log = open(os.path.join(data, "server.log"), "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "spikes.remote_bridge.server:create",
                             "--host", "127.0.0.1", "--port", str(p), "--workers", "1", "--log-level", "warning"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{p}"
    res = {"load_avg_at_start": os.getloadavg(), "ncpu": os.cpu_count()}
    try:
        async with httpx.AsyncClient() as c:
            for _ in range(100):
                try:
                    if (await c.get(base + "/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
            for mode in ("step", "event"):
                await c.post(f"{base}/spike/mode/{mode}")
                await c.post(f"{base}/spike/stats/reset")
                r = {"page": summ(await bench_page(base, a.n))}
                r["page_commit_ms"] = (await c.get(f"{base}/spike/stats")).json()
                await c.post(f"{base}/spike/stats/reset")
                r["stream_1"] = summ(await bench_stream(base, a.stream_s, 1))
                r["stream_1_commit_ms"] = (await c.get(f"{base}/spike/stats")).json()
                res[mode] = r
                print(mode, json.dumps(r), flush=True)
            # Q5: event mode only, 1 / 3 / 20 viewers + 3 hosts' presence heartbeats
            await c.post(f"{base}/spike/mode/event")
            q5 = {}
            for v in (1, 3, 20):
                await c.post(f"{base}/spike/stats/reset")
                lat = await bench_stream(base, a.stream_s, v)
                q5[v] = {"frame_ms": summ(lat), "commit_ms": (await c.get(f"{base}/spike/stats")).json()}
                print("q5", v, json.dumps(q5[v]), flush=True)
            res["q5"] = q5
    finally:
        proc.terminate(); proc.wait(10)
    res["load_avg_at_end"] = os.getloadavg()
    json.dump(res, open(a.out, "w"), indent=1)
    print("written", a.out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--stream-s", type=int, default=30)
    ap.add_argument("--page-kb", type=int, default=60)
    ap.add_argument("--out", default="transport-results.json")
    asyncio.run(main(ap.parse_args()))
