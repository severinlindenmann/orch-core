// Terminals (issue #40). The server renders every screen as escaped HTML and says what each agent is doing; this file
// swaps in fresh ones from the event stream, sizes the screen, keeps "changed since you looked" per browser, and, in
// Type and CLI modes only, posts keys back. Watch sends no keys; every mode fits the tmux window to the view.
(() => {
  "use strict";

  // ---- small helpers ----------------------------------------------------------------------------------------------
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (_) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode: per-page only */ } },
  };
  const SEEN = "orch.terminals.seen"; // name -> the sig this browser last saw on that terminal's own page
  const LABELS = { waiting: "waiting for you", busy: "busy", idle: "idle" };
  const ago = (epoch) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - Number(epoch)));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.round(s / 60) + " min";
    if (s < 86400) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
  };
  const tickAgo = () => document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = ago(el.dataset.ago); });
  tickAgo();
  setInterval(tickAgo, 5000);
  const setState = (el, status) => {
    if (!el) return;
    el.textContent = LABELS[status] || "running";
    el.className = el.className.replace(/term-state-\S+/, "term-state-" + (status || "none"));
  };
  const lineOf = (l) => [l.ticket ? l.ticket + (l.tasks ? " " + l.tasks + " tasks" : "") + (l.more ? " +" + l.more : "") : "",
    l.subs, l.context ? "context " + l.context : "", l.cache].filter(Boolean).join(" · ");

  // A live stream that rests while the tab is hidden: closed on hide (the server stops polling tmux for it), opened
  // again on show, where the first event brings the screen up to date. stop() ends it for good (the session ended).
  const live = (url, handlers) => {
    let es = null;
    let stopped = false;
    const open = () => {
      if (es || stopped || document.hidden) return;
      es = new EventSource(url);
      for (const [name, fn] of Object.entries(handlers)) es.addEventListener(name, fn);
    };
    const close = () => { if (es) { es.close(); es = null; } };
    document.addEventListener("visibilitychange", () => (document.hidden ? close() : open()));
    window.addEventListener("pagehide", close);
    open();
    return { stop() { stopped = true; close(); } };
  };

  // ---- sizes: "whole" scales the whole tmux window into its box; "readable" keeps a chosen font and follows the bottom
  const metrics = {};
  const charBox = (pre) => {
    const key = getComputedStyle(pre).fontFamily;
    if (!metrics[key]) {
      const probe = document.createElement("span");
      probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre;font-size:100px;line-height:1.2";
      probe.textContent = "MMMMMMMMMM";
      pre.appendChild(probe);
      metrics[key] = { w: probe.getBoundingClientRect().width / 1000, h: 1.2 };
      probe.remove();
    }
    return metrics[key];
  };
  const inner = (pre) => {
    const s = getComputedStyle(pre);
    return { w: pre.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight),
      h: pre.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom) };
  };
  const fitWhole = (pre) => {
    const cols = Number(pre.dataset.cols) || 80;
    const rows = Number(pre.dataset.rows) || 24;
    const box = inner(pre);
    if (box.w <= 0 || box.h <= 0) return;
    const m = charBox(pre);
    pre.style.fontSize = Math.max(1, Math.min(16, box.w / (cols * m.w), box.h / (rows * m.h))).toFixed(2) + "px";
  };

  // ---- the overview ---------------------------------------------------------------------------------------------------
  const grid = document.querySelector("[data-term-grid]") || document.querySelector("[data-factory-grid]");
  if (grid) {
    const tiles = () => [...grid.querySelectorAll("[data-tile]")];
    let filter = "all";
    const changed = (tile, seen) => tile.dataset.tile in seen && seen[tile.dataset.tile] !== tile.dataset.sig;
    const mark = () => {
      const seen = store.get(SEEN, {});
      let nChanged = 0;
      const attention = [];
      for (const tile of tiles()) {
        const isChanged = changed(tile, seen);
        tile.querySelector("[data-unread]").hidden = !isChanged;
        tile.classList.toggle("is-changed", isChanged);
        if (isChanged) nChanged += 1;
        if (tile.dataset.status === "waiting" || isChanged) attention.push(tile);
        tile.hidden = !(filter === "all" || (filter === "changed" ? isChanged : tile.dataset.status === filter));
      }
      const n = document.querySelector("[data-changed-n]");
      if (n) n.textContent = nChanged;
      const next = document.querySelector("[data-next]");
      if (next) {
        next.hidden = attention.length === 0;
        if (attention.length) next.href = attention[0].getAttribute("href");
        next.querySelector("[data-next-n]").textContent = "· " + attention.length;
      }
    };
    for (const b of document.querySelectorAll("[data-filter]")) {
      b.addEventListener("click", () => {
        filter = b.dataset.filter;
        document.querySelectorAll("[data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        mark();
      });
    }
    live("/terminals/stream", { screens: (e) => {
      for (const [name, screen] of Object.entries(JSON.parse(e.data))) {
        const pre = document.querySelector('[data-screen="' + CSS.escape(name) + '"]'); // factory tiles too
        if (pre) pre.innerHTML = screen.tail; // escaped by the server (terminals.ansi_to_html)
      }
    }, infos: (e) => {
      for (const [name, l] of Object.entries(JSON.parse(e.data))) {
        const tile = grid.querySelector('[data-tile="' + CSS.escape(name) + '"]');
        if (!tile) continue;
        tile.dataset.status = l.status;
        tile.dataset.sig = l.sig;
        tile.querySelector('[data-f="title"]').textContent = l.title || name;
        setState(tile.querySelector('[data-f="state"]'), l.status);
        const now = tile.querySelector('[data-f="now"]');
        now.textContent = l.now || "No agent activity read yet";
        now.className = "term-now term-now-" + (l.kind || "none");
        tile.querySelector('[data-f="ticket"]').textContent = l.ticket || "no ticket";
        tile.querySelector('[data-f="tasks"]').textContent = l.tasks ? l.tasks + " tasks" : "";
        tile.querySelector('[data-f="subs"]').textContent = l.subs;
        const cache = tile.querySelector('[data-f="cache"]');
        if (cache) {  // a page from before the cache field
          cache.textContent = l.cache;
          cache.classList.toggle("term-cache-cold", l.cold);
        }
        const age = tile.querySelector("[data-ago]");
        age.dataset.ago = l.activity;
        age.textContent = ago(l.activity);
      }
      mark();
    }, sessions: () => location.reload() });
    window.addEventListener("storage", mark); // looked at in another tab
    mark();
    return;
  }

  // ---- one terminal -------------------------------------------------------------------------------------------------
  const term = document.querySelector("[data-term]");
  if (!term) return;
  const name = term.dataset.term;
  const base = term.dataset.base || "/terminals/" + encodeURIComponent(name); // a factory session: /factory-sessions/
  const pre = term.querySelector(".term-screen");
  const stateText = term.querySelector(".term-state-text");
  const moreCols = term.querySelector("[data-more-cols]");
  const jump = term.querySelector("[data-jump]");
  const summary = document.querySelector("[data-summary]");

  const seeNow = (sig) => { const seen = store.get(SEEN, {}); seen[name] = sig; store.set(SEEN, seen); };
  seeNow(summary.dataset.sig); // opening it is looking at it

  let view = store.get("orch.terminals.view", "readable");
  let size = Number(store.get("orch.terminals.size", 14)) || 14;
  let follow = true;
  const typing = () => term.dataset.mode === "type" || term.dataset.mode === "cli";
  // CLI: every key pressed on the page goes to the session (except in a form field); Type: only on the focused screen
  const takesKeys = (e) => term.dataset.mode === "cli" ? !e.target.closest("input, textarea, select") : term.dataset.mode === "type" && e.target === pre;

  const indicators = () => {
    if (view !== "readable") { moreCols.hidden = true; jump.hidden = true; return; }
    const hidden = pre.scrollWidth - pre.clientWidth - pre.scrollLeft;
    const cw = charBox(pre).w * size;
    moreCols.hidden = hidden < cw;
    if (!moreCols.hidden) moreCols.textContent = Math.round(hidden / cw) + " more cols →";
    jump.hidden = follow;
  };
  const layout = () => {
    term.dataset.view = view;
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    if (view === "whole") fitWhole(pre);
    else pre.style.fontSize = size + "px";
    if (view === "readable" && follow) pre.scrollTop = pre.scrollHeight;
    indicators();
  };
  pre.addEventListener("scroll", () => {
    if (view !== "readable") return;
    follow = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    indicators();
  });
  jump.addEventListener("click", () => { follow = true; layout(); });

  const post = (path, body) => fetch(base + path, {
    method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => { if (!r.ok) r.text().then((t) => { stateText.textContent = t; }); }).catch(() => {});

  // The tmux window takes the size that fills this view at the chosen font, watching or typing, so the agent draws for
  // this screen (with two browsers on one session, the last one to resize wins). The phone keyboard shrinks the view:
  // the visual viewport says so.
  const sizeTmux = () => {
    if (term.dataset.mode === "gone") return;
    if (term.dataset.base && !typing()) return; // a factory session: watching never resizes the runner's pane
    const m = charBox(pre);
    const font = view === "readable" ? size : 13;
    const box = inner(pre);
    if (box.w <= 0 || box.h <= 0) return;
    const want = { cols: Math.floor(box.w / (font * m.w)), rows: Math.floor(box.h / (font * m.h)) };
    if (want.cols === Number(pre.dataset.cols) && want.rows === Number(pre.dataset.rows)) return; // already that size
    if (sent && sent.cols === want.cols && sent.rows === want.rows) return;
    sent = want;
    post("/size", want);
  };
  let sent = null; // the last size asked for, so a burst of resize events asks once
  let fitTimer = null;
  const refit = () => { clearTimeout(fitTimer); fitTimer = setTimeout(() => { layout(); sizeTmux(); }, 150); };
  window.addEventListener("resize", refit);
  window.addEventListener("orientationchange", refit);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", refit);

  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    view = b.dataset.view;
    store.set("orch.terminals.view", view);
    follow = true;
    paint();
    layout();
    sizeTmux();
  }));
  document.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
    size = Math.max(9, Math.min(22, size + Number(b.dataset.zoom)));
    store.set("orch.terminals.size", size);
    if (view !== "readable") { view = "readable"; store.set("orch.terminals.view", view); paint(); }
    layout();
    sizeTmux();
  }));

  let lastScreen = null;
  const paint = () => {
    if (!lastScreen) return;
    // escaped by the server (terminals.ansi_to_html); only <span style> is added. Readable leaves out the blank rows
    // under the last output, so following the bottom shows the live part; Whole pane is the window row for row.
    pre.innerHTML = view === "whole" ? lastScreen.html : lastScreen.trim;
    pre.dataset.cols = lastScreen.cols;
    pre.dataset.rows = lastScreen.rows;
  };
  const stream = live(base + "/stream", { screen: (e) => {
    lastScreen = JSON.parse(e.data);
    paint();
    layout();
  }, info: (e) => {
    const l = JSON.parse(e.data);
    summary.querySelector('[data-f="now"]').textContent = l.now || "No agent activity read yet";
    summary.querySelector('[data-f="line"]').textContent = lineOf(l);
    setState(document.querySelector('.page-head [data-f="state"]'), l.status);
    summary.dataset.sig = l.sig;
    seeNow(l.sig); // watching it counts as looking
  }, gone: () => {
    stream.stop();
    term.dataset.mode = "gone";
    stateText.textContent = "ended";
  } });

  // Keys go out in order, batched: consecutive characters become one text item. Type mode only.
  let queue = [];
  let timer = null;
  const flush = () => { timer = null; if (queue.length) post("/keys", { seq: queue.splice(0) }); };
  const push = (item) => {
    if (!typing()) return;
    const last = queue[queue.length - 1];
    if (item.text && last && last.text) last.text += item.text;
    else queue.push(item);
    if (!timer) timer = setTimeout(flush, 15);
  };

  const setMode = (mode) => {
    if (term.dataset.mode === "gone") return;
    term.dataset.mode = mode;
    for (const b of document.querySelectorAll("[data-mode]")) b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
    stateText.textContent = { type: "you are typing", cli: "CLI · every key goes to the session" }[mode] || "watching · read-only";
    // Type puts the cursor in the reply field; keys go straight to the agent only after a click on the screen itself.
    // CLI has no reply field: the screen takes the focus and the keys.
    if (mode === "type") { follow = true; send.elements.text.focus(); refit(); }
    else if (mode === "cli") { follow = true; pre.focus(); refit(); }
    else layout();
  };
  for (const b of document.querySelectorAll("[data-mode]")) b.addEventListener("click", () => setMode(b.dataset.mode));

  // Details panel: open on wide screens, closed on phones, then as this browser last left it.
  const details = document.getElementById("term-details");
  const detailsBtn = document.querySelector("[data-details]");
  const setDetails = (open, remember) => {
    details.hidden = !open;
    detailsBtn.setAttribute("aria-expanded", String(open));
    detailsBtn.textContent = open ? "Hide details" : "Details";
    if (remember) store.set("orch.terminals.details", open);
    refit();
  };
  setDetails(store.get("orch.terminals.details", window.innerWidth >= 1100), false);
  detailsBtn.addEventListener("click", () => setDetails(details.hidden, true));

  // Fullscreen: the browser's own (landscape on phones that allow it), else the whole window (iPhone Safari).
  const full = term.querySelector("[data-full]");
  const isFull = () => document.fullscreenElement === term || term.classList.contains("is-full");
  const markFull = (on) => {
    term.classList.toggle("is-full", on && document.fullscreenElement !== term);
    document.documentElement.classList.toggle("term-full-open", on);
    full.setAttribute("aria-pressed", String(on));
    full.setAttribute("aria-label", on ? "Exit fullscreen" : "Fullscreen");
    full.title = full.getAttribute("aria-label");
    refit();
  };
  full.addEventListener("click", async () => {
    if (isFull()) {
      if (document.fullscreenElement) await document.exitFullscreen().catch(() => {});
      markFull(false);
      return;
    }
    if (term.requestFullscreen) {
      try {
        // some browsers never settle the promise (an iframe without allow=fullscreen): give up after a second
        await Promise.race([term.requestFullscreen({ navigationUI: "hide" }),
          new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 1000))]);
        if (screen.orientation && screen.orientation.lock) screen.orientation.lock("landscape").catch(() => {});
        markFull(true);
        return;
      } catch (_) { /* not allowed here: fall back to the whole window */ }
    }
    markFull(true);
  });
  document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && full.getAttribute("aria-pressed") === "true") markFull(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && term.classList.contains("is-full") && !typing()) markFull(false); });

  const NAMED = { Enter: "Enter", Escape: "Escape", Backspace: "BSpace", Delete: "DC", ArrowUp: "Up", ArrowDown: "Down",
    ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End", PageUp: "PPage", PageDown: "NPage" };
  document.addEventListener("keydown", (e) => {
    if (!takesKeys(e) || e.metaKey || e.isComposing) return;
    let item = null;
    if (e.key === "Tab") item = { key: e.shiftKey ? "BTab" : "Tab" };
    else if (NAMED[e.key]) item = { key: NAMED[e.key] };
    else if (e.ctrlKey && /^[a-z]$/i.test(e.key)) item = { key: "C-" + e.key.toLowerCase() };
    else if (e.key.length === 1 && !e.ctrlKey && !e.altKey) item = { text: e.key };
    if (!item) return;
    e.preventDefault();
    push(item);
  });
  document.addEventListener("paste", (e) => {
    if (!takesKeys(e)) return;
    e.preventDefault();
    const text = (e.clipboardData.getData("text") || "").replace(/\r\n?/g, "\n");
    text.split("\n").forEach((line, i) => {
      if (i) push({ key: "Enter" });
      if (line) push({ text: line });
    });
  });

  // The key buttons and the reply field are shown in Type mode only (and push() refuses in Watch anyway).
  for (const b of term.querySelectorAll("[data-key]")) b.addEventListener("click", () => push({ key: b.dataset.key }));
  const send = term.querySelector("[data-term-send]");
  send.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!typing()) return;
    const text = send.elements.text.value;
    if (text) push({ text });
    push({ key: "Enter" });
    send.elements.text.value = "";
  });

  layout();
  sizeTmux(); // fit the session to this view from the start, watching too
  if (window.innerHeight < 500) term.scrollIntoView({ block: "start" }); // a landscape phone: the terminal first
})();
