// R0 spike (throwaway). The TIX-origin parent: owns the frame, relays its requests, keeps history.
(() => {
  const q = new URLSearchParams(location.search);
  const START = q.get("path") || "/";
  const tok = crypto.randomUUID();
  const log = (window.__log = []);
  window.__relayMs = [];
  const frame = document.createElement("iframe");
  frame.name = "dash";
  frame.setAttribute("sandbox", "allow-scripts allow-forms");   // no allow-same-origin
  frame.src = "/frame?tok=" + tok;
  frame.style.cssText = "width:100%;height:calc(100vh - 40px);border:0";
  frame.addEventListener("load", () => log.push({ at: Math.round(performance.now()), t: "iframe-load" }));
  document.body.appendChild(frame);
  const send = (m) => frame.contentWindow.postMessage(Object.assign({ k: "orch" }, m), "*");
  window.__go = (p) => send({ t: "go", path: p });   // test hook: ask the shim to open a page
  const streams = new Map();
  let acked = false;
  const enc = (o) => encodeURIComponent(JSON.stringify(o));

  addEventListener("message", async (e) => {
    if (e.source !== frame.contentWindow || !e.data || e.data.k !== "orch") return;
    const m = e.data;
    const { body, ...rest } = m;
    log.push(Object.assign({ at: Math.round(performance.now()) }, rest));
    if (m.t === "ready") { if (m.tok === tok && !acked) { acked = true; send({ t: "go", path: START }); } return; }
    if (!acked) return;
    if (m.t === "req") {
      const t0 = performance.now();
      try {
        const r = await fetch("/relay", { method: "POST", headers: { "x-spike-req": enc({ method: m.method, path: m.path, headers: m.headers }) }, body: m.body });
        const meta = JSON.parse(decodeURIComponent(r.headers.get("x-spike-res")));
        const buf = await r.arrayBuffer();
        window.__relayMs.push(performance.now() - t0);
        send({ t: "res", id: m.id, status: meta.status, headers: meta.headers, url: meta.url, body: buf });
      } catch (err) { send({ t: "res", id: m.id, error: String(err) }); }
    } else if (m.t === "push") history.pushState({ path: m.path }, "", "#" + m.path);
    else if (m.t === "replace") history.replaceState({ path: m.path }, "", "#" + m.path);
    else if (m.t === "navigated") { if (m.push) history.pushState({ path: m.path }, "", "#" + m.path); else history.replaceState({ path: m.path }, "", "#" + m.path); }
    else if (m.t === "hist") history[m.op === "back" ? "back" : m.op === "forward" ? "forward" : "go"](m.op);
    else if (m.t === "sopen") {
      const ac = new AbortController();
      streams.set(m.id, ac);
      try {
        const r = await fetch("/relay-stream?path=" + encodeURIComponent(m.path), { signal: ac.signal });
        send({ t: "sdata", id: m.id, chunk: "" });
        const rd = r.body.getReader(), dec = new TextDecoder();
        for (;;) { const { done, value } = await rd.read(); if (done) break; send({ t: "sdata", id: m.id, chunk: dec.decode(value, { stream: true }) }); }
      } catch (_) { /* aborted */ }
      send({ t: "send", id: m.id });
    } else if (m.t === "sclose") { const ac = streams.get(m.id); if (ac) ac.abort(); streams.delete(m.id); }
  });
  addEventListener("popstate", (e) => { const p = (e.state && e.state.path) || START; send({ t: "go", path: p }); });
  document.getElementById("back").onclick = () => history.back();
})();
