// R0 spike (throwaway). The frame shim: runs first, inline with the frame's nonce.
// Everything the dashboard asks the network for goes to the parent by postMessage.
(() => {
  "use strict";
  const NONCE = document.currentScript.nonce;
  const TOKEN = document.currentScript.dataset.tok;
  const OrigRequest = Request, OrigResponse = Response, origReplace = History.prototype.replaceState;
  // FINDING: every dashboard page carries its own CSP (PAGE_CSP), so "no CSP of its own" would reject all pages.
  // The real rule must be "the host tags a response as a dashboard page", not "the response has no CSP".
  const PAGE_CSP_PREFIX = "default-src 'self'; script-src 'self'";
  const toParent =(m) => parent.postMessage(Object.assign({ k: "orch" }, m), location.origin);
  const pending = new Map();
  let seq = 0;
  const call = (method, path, headers, body) => new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    toParent({ t: "req", id, method, path, headers: headers || {}, body: body || null });
  });
  const here = () => location.pathname + location.search;

  // ---- fetch / XHR / EventSource -------------------------------------------------------------
  const sameOrigin = (u) => u.origin === location.origin;
  window.fetch = async (input, init) => {
    const req = new OrigRequest(input, init);
    const u = new URL(req.url);
    if (!sameOrigin(u)) throw new TypeError("blocked: not the dashboard");
    const body = ["GET", "HEAD"].includes(req.method) ? null : await req.arrayBuffer();
    const headers = {};
    req.headers.forEach((v, k) => { headers[k] = v; });
    const r = await call(req.method, u.pathname + u.search, headers, body);
    const nullBody = [101, 204, 205, 304].includes(r.status);
    const resp = new OrigResponse(nullBody ? null : r.body, { status: r.status, headers: r.headers });
    Object.defineProperty(resp, "url", { value: location.origin + r.url });
    Object.defineProperty(resp, "redirected", { value: r.url !== u.pathname + u.search });
    return resp;
  };
  window.XMLHttpRequest = function () { throw new Error("XMLHttpRequest is not available in the frame"); };

  const streams = new Map();
  window.EventSource = class extends EventTarget {
    constructor(url) {
      super();
      this.url = new URL(url, location.href).href;
      this.readyState = 0;
      this._id = ++seq;
      this._buf = "";
      this._ev = "message";
      this._data = [];
      streams.set(this._id, this);
      toParent({ t: "sopen", id: this._id, path: new URL(this.url).pathname + new URL(this.url).search });
    }
    close() { this.readyState = 2; streams.delete(this._id); toParent({ t: "sclose", id: this._id }); }
    _feed(chunk) {
      this._buf += chunk;
      let i;
      while ((i = this._buf.indexOf("\n")) >= 0) {
        const line = this._buf.slice(0, i).replace(/\r$/, "");
        this._buf = this._buf.slice(i + 1);
        if (line === "") {
          if (this._data.length) this._emit(this._ev, this._data.join("\n"));
          this._ev = "message"; this._data = [];
        } else if (line.startsWith("event:")) this._ev = line.slice(6).trim();
        else if (line.startsWith("data:")) this._data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    _emit(type, data) {
      const e = new MessageEvent(type, { data });
      const h = this["on" + type];
      if (typeof h === "function") h.call(this, e);
      this.dispatchEvent(e);
    }
    _open() { this.readyState = 1; const e = new Event("open"); if (this.onopen) this.onopen(e); this.dispatchEvent(e); }
    _err() { const e = new Event("error"); if (this.onerror) this.onerror(e); this.dispatchEvent(e); }
  };
  window.open = (href) => { toParent({ t: "external", href: String(href) }); return null; };

  // ---- storage / cookie: opaque origin throws on all of them; give the page an in-memory stand-in ----
  const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), clear: () => m.clear() }; };
  for (const name of ["localStorage", "sessionStorage"]) {
    try { const s = mem(); Object.defineProperty(window, name, { configurable: true, get: () => s }); } catch (e) { toParent({ t: "log", m: name + " not redefinable: " + e.message }); }
  }
  try {
    let jar = "";
    Object.defineProperty(Document.prototype, "cookie", { configurable: true, get: () => jar, set: (v) => { jar = String(v).split(";")[0]; } });
  } catch (e) { toParent({ t: "log", m: "cookie not redefinable: " + e.message }); }

  // ---- history: the TIX app owns it; the frame's own URL is kept truthful with replaceState only ----
  const lie = (url) => {
    const u = new URL(url || location.href, location.href);
    try { origReplace.call(history, null, "", u.pathname + u.search + u.hash); } catch (e) { toParent({ t: "log", m: "replaceState: " + e.name }); }
    return u.pathname + u.search + u.hash;
  };
  History.prototype.pushState = function (s, t, url) { toParent({ t: "push", path: lie(url) }); };
  History.prototype.replaceState = function (s, t, url) { toParent({ t: "replace", path: lie(url) }); };
  History.prototype.back = () => toParent({ t: "hist", op: "back" });
  History.prototype.forward = () => toParent({ t: "hist", op: "forward" });
  History.prototype.go = (n) => toParent({ t: "hist", op: n });

  // ---- document writer ------------------------------------------------------------------------
  const cache = new Map();
  const blobs = new Map();
  let fetches = 0;
  const load = (path) => {
    if (!cache.has(path)) { fetches++; cache.set(path, call("GET", path).then((r) => { if (r.status !== 200) throw new Error(path + " " + r.status); return r; })); }
    return cache.get(path);
  };
  const blobUrl = async (path) => {
    if (!blobs.has(path)) blobs.set(path, load(path).then((r) => URL.createObjectURL(new Blob([r.body], { type: r.headers["content-type"] || "" }))));
    return blobs.get(path);
  };
  const local = (v, base) => { try { const u = new URL(v, base); return sameOrigin(u) ? u.pathname + u.search : null; } catch (_) { return null; } };
  const text = (r) => new TextDecoder().decode(r.body);

  async function cssWithBlobs(css, basePath) {
    const urls = [...css.matchAll(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g)].map((m) => m[2]).filter((u) => !u.startsWith("data:"));
    const map = new Map();
    await Promise.all([...new Set(urls)].map(async (u) => { const p = local(u, location.origin + basePath); if (p) { try { map.set(u, await blobUrl(p)); } catch (_) { /* leave */ } } }));
    return css.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (m, q, u) => (map.has(u) ? `url("${map.get(u)}")` : m));
  }

  async function render(html, path) {
    const t0 = performance.now();
    const before = fetches;
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script:not([src])").forEach((s) => s.remove());           // strip foreign scripts
    doc.querySelectorAll("[onclick],[onsubmit],[onchange],[onload],[onerror]").forEach((e) => ["onclick", "onsubmit", "onchange", "onload", "onerror"].forEach((a) => e.removeAttribute(a)));
    doc.querySelectorAll("link[rel~=icon],link[rel=preload],link[rel=modulepreload]").forEach((l) => l.remove());
    const jobs = [];
    const late = [];
    doc.querySelectorAll("script[src]").forEach((s) => {
      const p = local(s.getAttribute("src"), location.origin + path);
      const deferred = s.hasAttribute("defer") || s.hasAttribute("async");
      const inline = doc.createElement("script");
      inline.setAttribute("nonce", NONCE);
      s.replaceWith(inline);
      if (!p) { inline.remove(); return; }
      jobs.push(load(p).then((r) => { inline.textContent = text(r); }));
      if (deferred) { late.push(inline); inline.remove(); }
    });
    doc.querySelectorAll("link[rel=stylesheet]").forEach((l) => {
      const p = local(l.getAttribute("href"), location.origin + path);
      const st = doc.createElement("style");
      l.replaceWith(st);
      if (!p) { st.remove(); return; }
      jobs.push(load(p).then(async (r) => { st.textContent = await cssWithBlobs(text(r), p); }));
    });
    doc.querySelectorAll("img[src]").forEach((im) => {
      const p = local(im.getAttribute("src"), location.origin + path);
      if (!p) { im.removeAttribute("src"); return; }
      jobs.push(blobUrl(p).then((u) => im.setAttribute("src", u), () => im.removeAttribute("src")));
    });
    doc.querySelectorAll("iframe").forEach((f) => f.remove());                     // frame-src 'none' anyway
    await Promise.all(jobs);
    late.forEach((s) => doc.body.appendChild(s));
    lie(path);
    // FINDING: document.open() keeps the window, so the old page's open streams, timers and window listeners survive it.
    // Without this, leaked streams filled the browser's per-host connection limit and every later request queued.
    [...streams.values()].forEach((s) => s.close());
    document.open();
    document.write("<!doctype html>" + doc.documentElement.outerHTML);
    document.close();
    arm();
    toParent({ t: "rendered", path, ms: Math.round(performance.now() - t0), bridgeFetches: fetches - before, bytes: html.length });
  }

  async function navigate(method, path, body, headers, push = true) {
    const r = await call(method, path, headers, body);
    const ct = (r.headers["content-type"] || "").split(";")[0];
    if (r.status === 200 && ct === "text/html" && (!r.headers["content-security-policy"] || r.headers["content-security-policy"].startsWith(PAGE_CSP_PREFIX))) {
      await render(text(r), r.url);
      toParent({ t: "navigated", path: r.url, push });
    } else toParent({ t: "viewer", path: r.url, status: r.status, ct });
  }

  // ---- delegated handlers, re-armed after every document write ---------------------------------
  const onClick = (e) => {
    if (e.defaultPrevented) return;
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    const u = new URL(a.href, location.href);
    if (a.hasAttribute("download")) { e.preventDefault(); toParent({ t: "download", path: u.pathname + u.search }); return; }
    if (a.target === "_blank" || !sameOrigin(u)) { e.preventDefault(); toParent({ t: "external", href: a.href }); return; }
    if (u.pathname === location.pathname && u.search === location.search && u.hash) return;
    e.preventDefault();
    navigate("GET", u.pathname + u.search);
  };
  const submitForm = (form, submitter) => {
    const fd = new FormData(form, submitter);
    const method = (submitter && submitter.formMethod || form.method || "GET").toUpperCase();
    const u = new URL(submitter && submitter.formAction || form.action || location.href, location.href);
    if (method === "GET") { new URLSearchParams(fd).forEach((v, k) => u.searchParams.set(k, v)); return navigate("GET", u.pathname + u.search); }
    const r = new OrigRequest(location.origin + "/", { method: "POST", body: form.enctype === "multipart/form-data" ? fd : new URLSearchParams(fd) });
    return r.arrayBuffer().then((buf) => navigate("POST", u.pathname + u.search, buf, { "content-type": r.headers.get("content-type") }));
  };
  const onSubmit = (e) => { if (e.defaultPrevented) return; e.preventDefault(); submitForm(e.target, e.submitter); };
  HTMLFormElement.prototype.submit = function () { submitForm(this, null); };
  const onError = (e) => toParent({ t: "pageerror", m: String(e.message), at: (e.filename || "").slice(-30) + ":" + e.lineno });
  const imgObserver = new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach((n) => {
    if (!n.querySelectorAll) return;
    n.querySelectorAll("img[src]").forEach((im) => { const p = local(im.getAttribute("src"), location.href); if (p) blobUrl(p).then((u) => im.setAttribute("src", u)); });
  })));
  function arm() {
    window.addEventListener("error", onError, true);
    window.addEventListener("click", onClick, false);
    window.addEventListener("submit", onSubmit, false);
    window.addEventListener("message", onMessage, false);
    imgObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  function onMessage(e) {
    if (e.source !== parent || !e.data || e.data.k !== "orch") return;
    const m = e.data;
    if (m.t === "res") { const p = pending.get(m.id); pending.delete(m.id); if (p) (m.error ? p.reject(new TypeError(m.error)) : p.resolve(m)); }
    else if (m.t === "go") navigate("GET", m.path, null, null, false).catch((err) => toParent({ t: "log", m: "go failed " + err }));
    else if (m.t === "sdata") { const s = streams.get(m.id); if (s) { if (s.readyState === 0) s._open(); if (m.chunk) s._feed(m.chunk); } }
    else if (m.t === "send") { const s = streams.get(m.id); if (s) { s.readyState = 2; s._err(); } }
  }
  arm();
  toParent({ t: "ready", tok: TOKEN });
})();
