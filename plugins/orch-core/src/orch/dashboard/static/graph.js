// Graph page (templates/graph.html): draws the JSON block #graph-data (orch.core.graph.build) as an SVG.
// Views: "code" (tickets and the files their commits changed, files grouped by folder), "deps" (one lane per epic,
// arrows from blocker to blocked) and "local" (two hops around one ticket). No library: a small force layout runs a
// fixed number of steps before the first paint, so nothing moves on its own (and reduced motion needs no special case).
// Ticket text is untrusted: it only ever goes into textContent and attributes, never into HTML.
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const ROLE = { backlog: "neu", open: "neu", "in-progress": "info", waiting: "you", testing: "info", done: "ok" };
  const LABEL = { backlog: "Backlog", open: "Open", "in-progress": "In progress", waiting: "Waiting", testing: "Testing", done: "Done" };

  const el = (tag, attrs, ...kids) => {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v); });
    kids.flat().forEach((k) => { if (k !== null && k !== undefined && k !== false) node.append(k instanceof Node ? k : String(k)); });
    return node;
  };
  const svgEl = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v !== null && v !== undefined) node.setAttribute(k, v); });
    return node;
  };
  const icon = (role) => {
    const s = svgEl("svg", { class: "i", "aria-hidden": "true" });
    s.append(svgEl("use", { href: "#i-" + role }));
    return s;
  };
  const chip = (role, text) => el("span", { class: "chip chip-" + role }, icon(role), " " + text);

  // a small seeded random, so the same workspace lays out the same way on every load
  const rng = (seed) => () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };

  function start() {
    const fig = document.getElementById("graph");
    const blob = document.getElementById("graph-data");
    if (!fig || !blob || fig.dataset.ready) return;
    fig.dataset.ready = "1";
    const svg = document.getElementById("graph-svg");
    const hint = document.getElementById("graph-hint");
    const inspector = document.getElementById("graph-inspector");
    let data;
    try { data = JSON.parse(blob.textContent); } catch (e) { hint.textContent = "The graph data did not load."; return; }

    // ---------- model ----------
    const tickets = new Map();
    const files = new Set();
    data.nodes.forEach((n) => { if (n.kind === "ticket") tickets.set(n.id, n); else files.add(n.id); });
    const tf = new Map(); // ticket -> Map(file -> commits)
    const ft = new Map(); // file -> Map(ticket -> commits)
    const links = [];     // ticket-to-ticket edges
    data.edges.forEach((e) => {
      if (e.kind === "changed") {
        if (!tf.has(e.source)) tf.set(e.source, new Map());
        if (!ft.has(e.target)) ft.set(e.target, new Map());
        tf.get(e.source).set(e.target, e.weight);
        ft.get(e.target).set(e.source, e.weight);
      } else if (tickets.has(e.source) && tickets.has(e.target)) links.push(e);
    });
    const collide = new Set(data.collisions.map((c) => c.file));
    const isOpen = (id) => tickets.get(id).status !== "done";
    const folderOf = (f) => { const s = f.lastIndexOf("/"); if (s >= 0) return f.slice(0, s); const c = f.indexOf(":"); return c >= 0 ? f.slice(0, c + 1) : "."; };
    const base = (f) => f.slice(Math.max(f.lastIndexOf("/"), f.indexOf(":")) + 1);
    const short = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");

    const params = new URLSearchParams(location.search);
    const state = { view: fig.dataset.view || "code", focus: fig.dataset.focus || "", sel: fig.dataset.focus || "", q: "", epic: "", done: true, only: false };
    if (!state.focus) {
      const hot = (id) => [...tf.get(id).keys()].filter((f) => collide.has(f)).length;  // a ticket in a collision first
      const busy = [...tickets.values()].filter((t) => isOpen(t.id) && tf.has(t.id))
        .sort((a, b) => hot(b.id) - hot(a.id) || tf.get(b.id).size - tf.get(a.id).size || a.id.localeCompare(b.id))[0];
      state.focus = busy ? busy.id : "";
    }

    // ---------- the graph for the current view ----------
    function build() {
      const kept = [...tickets.values()].filter((t) => (state.done || t.status !== "done") && (!state.epic || t.epic === state.epic || t.id === state.epic));
      const keep = new Set(kept.map((t) => t.id));
      let nodes = [];
      let edges = [];
      const tlinks = () => links.filter((l) => keep.has(l.source) && keep.has(l.target)).map((l) => ({ source: l.source, target: l.target, kind: l.kind }));
      if (state.view === "deps") {
        nodes = kept.map((t) => ({ id: t.id, kind: "ticket", t }));
        edges = tlinks();
        return { nodes, edges };
      }
      const fs = [...files].filter((f) => [...(ft.get(f) || new Map()).keys()].some((k) => keep.has(k)) && (!state.only || collide.has(f)));
      const fset = new Set(fs);
      let ts = kept.filter((t) => [...(tf.get(t.id) || new Map()).keys()].some((f) => fset.has(f)) && (!state.only || isOpen(t.id)));
      const tset = new Set(ts.map((t) => t.id));
      if (!state.only) { // epics and linked tickets of what is shown, so the code map still says where work belongs
        tlinks().forEach((l) => { if (tset.has(l.source) || tset.has(l.target)) { tset.add(l.source); tset.add(l.target); } });
        ts = kept.filter((t) => tset.has(t.id));
        edges = tlinks().filter((l) => tset.has(l.source) && tset.has(l.target));
      }
      nodes = ts.map((t) => ({ id: t.id, kind: "ticket", t }));
      ts.forEach((t) => (tf.get(t.id) || new Map()).forEach((w, f) => {
        if (fset.has(f)) edges.push({ source: t.id, target: f, kind: collide.has(f) && isOpen(t.id) ? "collide" : "changed", weight: w });
      }));
      const used = new Set(edges.map((e) => e.target));
      const shown = fs.filter((f) => used.has(f));
      [...new Set(shown.map(folderOf))].forEach((d) => nodes.push({ id: "dir:" + d, kind: "folder", label: d }));
      shown.forEach((f) => { nodes.push({ id: f, kind: "file", folder: "dir:" + folderOf(f) }); edges.push({ source: "dir:" + folderOf(f), target: f, kind: "infolder" }); });
      if (state.view === "local" && state.focus) {
        const adj = new Map();
        edges.filter((e) => e.kind !== "infolder").forEach((e) => {
          if (!adj.has(e.source)) adj.set(e.source, new Set());
          if (!adj.has(e.target)) adj.set(e.target, new Set());
          adj.get(e.source).add(e.target); adj.get(e.target).add(e.source);
        });
        const seen = new Set([state.focus]);
        let ring = [state.focus];
        for (let hop = 0; hop < 2; hop += 1) {
          const next = [];
          ring.forEach((x) => (adj.get(x) || []).forEach((y) => { if (!seen.has(y)) { seen.add(y); next.push(y); } }));
          ring = next;
        }
        if (!nodes.some((n) => n.id === state.focus) && tickets.has(state.focus)) nodes.push({ id: state.focus, kind: "ticket", t: tickets.get(state.focus) });
        nodes.filter((n) => n.kind === "file" && seen.has(n.id)).forEach((n) => seen.add(n.folder));
        nodes = nodes.filter((n) => seen.has(n.id));
        edges = edges.filter((e) => seen.has(e.source) && seen.has(e.target));
      }
      return { nodes, edges };
    }

    // ---------- layout ----------
    const radius = (n) => (n.t.type === "epic" ? 15 : 9 + Math.min(5, (tf.get(n.id) || new Map()).size));

    function layoutDeps(nodes) {
      const ids = new Set(nodes.map((n) => n.id));
      const depth = new Map();
      const d = (id, path = new Set()) => {
        if (depth.has(id)) return depth.get(id);
        if (path.has(id)) return 0; // a cycle in blocked_by: orch check reports it; draw it flat
        path.add(id);
        const blockers = links.filter((l) => l.kind === "blocks" && l.target === id && ids.has(l.source)).map((l) => d(l.source, path) + 1);
        const v = Math.max(0, ...blockers);
        depth.set(id, v);
        return v;
      };
      const epics = [...new Set(nodes.map((n) => n.t.epic || ""))].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
      const slots = new Map();
      nodes.forEach((n) => {
        n.lane = epics.indexOf(n.t.epic || "");
        n.col = n.t.type === "epic" ? 0 : 1 + d(n.id) + (n.t.status === "done" ? 0 : 1);
        const key = n.lane + ":" + n.col;
        n.k = (slots.get(key) || 0);
        slots.set(key, n.k + 1);
      });
      const rows = epics.map((_, i) => Math.max(1, ...[...slots].filter(([k]) => k.startsWith(i + ":")).map(([, v]) => v)));
      const COL = 230, ROW = 48, TOP = 44;
      let y = 0;
      const lanes = epics.map((e, i) => { const h = TOP + rows[i] * ROW + 8; const lane = { y, h, label: e ? `${e} ${(tickets.get(e) || {}).title || ""}` : "No epic" }; y += h + 14; return lane; });
      nodes.forEach((n) => { n.x = 40 + n.col * COL; n.y = lanes[n.lane].y + TOP + n.k * ROW + ROW / 2 - 8; });
      const cols = Math.max(...nodes.map((n) => n.col), 0);
      return lanes.map((l) => ({ ...l, w: 120 + cols * COL + 120 }));
    }

    function layoutForce(nodes, edges) {
      const rand = rng(nodes.length * 7919 + edges.length);
      const byId = new Map(nodes.map((n) => [n.id, n]));
      const folders = nodes.filter((n) => n.kind === "folder");
      const R = 70 * Math.sqrt(Math.max(folders.length, 1));
      folders.forEach((f, i) => { const a = (i / Math.max(folders.length, 1)) * Math.PI * 2; f.x = Math.cos(a) * R; f.y = Math.sin(a) * R; });
      nodes.forEach((n) => { // files first: a ticket starts at the middle of its files
        if (n.kind === "file") { const f = byId.get(n.folder); n.x = f.x + (rand() - 0.5) * 60; n.y = f.y + (rand() - 0.5) * 60; }
      });
      nodes.forEach((n) => {
        if (n.kind === "ticket") {
          const mine = edges.filter((e) => e.source === n.id && byId.has(e.target) && byId.get(e.target).kind === "file").map((e) => byId.get(e.target));
          n.x = mine.length ? mine.reduce((s, m) => s + m.x, 0) / mine.length + (rand() - 0.5) * 80 : (rand() - 0.5) * R * 2;
          n.y = mine.length ? mine.reduce((s, m) => s + m.y, 0) / mine.length + (rand() - 0.5) * 80 : (rand() - 0.5) * R * 2;
        }
        n.vx = 0; n.vy = 0;
      });
      const pinned = state.view === "local" ? byId.get(state.focus) : null;
      if (pinned) { pinned.x = 0; pinned.y = 0; }
      const charge = (n) => (n.kind === "ticket" ? 520 : n.kind === "folder" ? 140 : 220);
      const len = (e) => (e.kind === "infolder" ? 40 : e.kind === "parent" ? 120 : e.kind === "blocks" || e.kind === "follow_up" ? 110 : 90);
      const steps = nodes.length > 300 ? 160 : 320;
      for (let s = 0; s < steps; s += 1) {
        const alpha = 1 - s / steps;
        for (let i = 0; i < nodes.length; i += 1) {
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j += 1) {
            const b = nodes[j];
            let dx = a.x - b.x, dy = a.y - b.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) { dx = rand() - 0.5; dy = rand() - 0.5; d2 = 1; }
            if (d2 > 250000) continue;
            const f = (Math.sqrt(charge(a) * charge(b)) / d2) * alpha;
            a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
          }
        }
        edges.forEach((e) => {
          const a = byId.get(e.source), b = byId.get(e.target);
          const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
          const k = ((d - len(e)) / d) * (e.kind === "infolder" ? 0.2 : 0.06) * alpha;
          a.vx += dx * k; a.vy += dy * k; b.vx -= dx * k; b.vy -= dy * k;
        });
        nodes.forEach((n) => {
          n.vx -= n.x * 0.008 * alpha; n.vy -= n.y * 0.008 * alpha;
          const v = Math.hypot(n.vx, n.vy), cap = 30 * alpha + 2;
          if (v > cap) { n.vx *= cap / v; n.vy *= cap / v; }
          if (n !== pinned) { n.x += n.vx; n.y += n.vy; }
          n.vx *= 0.55; n.vy *= 0.55;
        });
      }
    }

    // ---------- drawing ----------
    let view = { x: 0, y: 0, k: 1 };
    let current = { nodes: [], edges: [] };
    let vp, edgeEls = [], nodeEls = new Map();
    const applyView = () => {
      if (!vp) return;
      vp.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`);
      svg.style.setProperty("--g-label", String(Math.min(1.6, Math.max(1, 1 / view.k))));  // labels stay readable zoomed out
    };

    function draw() {
      const g = build();
      current = g;
      svg.textContent = "";
      const defs = svgEl("defs");
      const marker = svgEl("marker", { id: "g-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
      marker.append(svgEl("path", { d: "M0,0L10,5L0,10z", class: "g-arrow" }));
      defs.append(marker);
      svg.append(defs);
      vp = svgEl("g");
      svg.append(vp);
      if (!g.nodes.length) {
        hint.textContent = state.view === "local" && !state.focus ? "Pick a ticket first." : "Nothing to draw with these filters.";
        edgeEls = []; nodeEls = new Map();
        legend(); count();
        return;
      }
      let lanes = [];
      if (state.view === "deps") lanes = layoutDeps(g.nodes); else layoutForce(g.nodes, g.edges);
      const byId = new Map(g.nodes.map((n) => [n.id, n]));
      g.edges.forEach((e) => { e.s = byId.get(e.source); e.t = byId.get(e.target); });

      const laneG = svgEl("g"); vp.append(laneG);
      lanes.forEach((l) => {
        laneG.append(svgEl("rect", { class: "g-lane", x: -10, y: l.y, width: l.w, height: l.h, rx: 12 }));
        const label = svgEl("text", { class: "g-lane-label", x: 6, y: l.y + 22 });
        label.textContent = short(l.label, 60);
        laneG.append(label);
      });
      const edgeG = svgEl("g"); vp.append(edgeG);
      edgeEls = g.edges.map((e) => {
        const line = svgEl("line", { class: "g-edge " + (e.kind === "infolder" ? "parent" : e.kind) });
        if (e.kind === "changed" || e.kind === "collide") line.style.strokeWidth = String(1 + Math.min(e.weight || 1, 6) * 0.5);
        if (e.kind === "blocks") line.setAttribute("marker-end", "url(#g-arrow)");
        edgeG.append(line);
        return { e, line };
      });
      const nodeG = svgEl("g"); vp.append(nodeG);
      nodeEls = new Map();
      g.nodes.forEach((n) => {
        const node = svgEl("g", { class: "g-node" });
        if (n.kind === "folder") {
          node.setAttribute("class", "g-folder-node");
          const t = svgEl("text", { class: "g-folder", "text-anchor": "middle", y: 4 });
          t.textContent = short(n.label, 28) + (n.label.endsWith(":") ? "" : "/");
          node.append(t);
        } else {
          node.setAttribute("tabindex", "0");
          node.setAttribute("role", "button");
          if (n.kind === "ticket") {
            const r = radius(n);
            node.setAttribute("aria-label", `${n.id} ${n.t.title || ""}, ${LABEL[n.t.status] || n.t.status}${n.t.needs ? ", needs you" : ""}`);
            node.append(svgEl("circle", { class: `g-body st-${n.t.status}${n.t.type === "epic" ? " epic" : ""}`, r }));
            if (n.t.needs) node.append(svgEl("circle", { class: "g-you", r: r + 4 }));
            const id = svgEl("text", { x: r + 6, y: -1 }); id.textContent = n.id; node.append(id);
            const sub = svgEl("text", { class: "g-sub", x: r + 6, y: 12 }); sub.textContent = short(n.t.title, state.view === "deps" ? 24 : 28); node.append(sub);
          } else {
            node.setAttribute("aria-label", `File ${n.id}${collide.has(n.id) ? ", changed by more than one open ticket" : ""}`);
            node.append(svgEl("rect", { class: "g-file" + (collide.has(n.id) ? " hot" : ""), x: -6, y: -6, width: 12, height: 12, rx: 3 }));
            const t = svgEl("text", { class: "g-sub", x: 10, y: 4 }); t.textContent = short(base(n.id), 32); node.append(t);
          }
          node.addEventListener("click", () => { if (!moved) select(n.id); });
          node.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(n.id); } });
          node.addEventListener("pointerenter", () => trace(n.id));
          node.addEventListener("pointerleave", () => trace(null));
          node.addEventListener("focus", () => trace(n.id));
          node.addEventListener("blur", () => trace(null));
          node.addEventListener("pointerdown", (ev) => startDrag(ev, n));
        }
        nodeG.append(node);
        nodeEls.set(n.id, { n, node });
      });
      place();
      mark();
      fit();
      hint.textContent = state.view === "deps" ? "Lanes are epics · arrows point from blocker to blocked"
        : state.view === "local" ? `Two links around ${state.focus} · drag nodes to untangle` : "Scroll to zoom · drag nodes or the background";
      legend(); count(); search();
    }

    function place() {
      edgeEls.forEach(({ e, line }) => {
        let x2 = e.t.x, y2 = e.t.y;
        if (e.kind === "blocks") {
          const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.hypot(dx, dy) || 1, r = radius(e.t) + 5;
          x2 = e.t.x - (dx / d) * r; y2 = e.t.y - (dy / d) * r;
        }
        line.setAttribute("x1", e.s.x); line.setAttribute("y1", e.s.y); line.setAttribute("x2", x2); line.setAttribute("y2", y2);
      });
      nodeEls.forEach(({ n, node }) => node.setAttribute("transform", `translate(${n.x},${n.y})`));
    }

    function mark() { // the selection ring
      nodeEls.forEach(({ n, node }) => {
        const old = node.querySelector(".g-sel");
        if (old) old.remove();
        if (n.id !== state.sel || n.kind === "folder") return;
        const ring = n.kind === "ticket"
          ? svgEl("circle", { class: "g-sel", r: radius(n) + (n.t.needs ? 9 : 5) })
          : svgEl("rect", { class: "g-sel", x: -10, y: -10, width: 20, height: 20, rx: 5 });
        node.insertBefore(ring, node.querySelector("text"));
      });
    }

    function fit() {
      const ns = current.nodes;
      if (!ns.length) return;
      const box = svg.getBoundingClientRect();
      const xs = ns.map((n) => n.x), ys = ns.map((n) => n.y);
      const x0 = Math.min(...xs) - (state.view === "deps" ? 60 : 30), x1 = Math.max(...xs) + 170, y0 = Math.min(...ys) - 40, y1 = Math.max(...ys) + 40;
      const k = Math.max(0.55, Math.min(1.3, Math.min(box.width / (x1 - x0), (box.height - 70) / (y1 - y0))));
      view = { k, x: box.width / 2 - k * (x0 + x1) / 2, y: (box.height - 50) / 2 - k * (y0 + y1) / 2 };
      applyView();
    }

    function neighbours(id) {
      const s = new Set([id]);
      current.edges.forEach((e) => { if (e.source === id) s.add(e.target); if (e.target === id) s.add(e.source); });
      return s;
    }
    function trace(id) {
      if (!id && state.q) { search(); return; }
      const nb = id ? neighbours(id) : null;
      nodeEls.forEach(({ n, node }) => node.classList.toggle("g-dim", Boolean(nb && !nb.has(n.id) && n.kind !== "folder")));
      edgeEls.forEach(({ e, line }) => line.classList.toggle("g-dim", Boolean(nb && e.source !== id && e.target !== id)));
    }
    function search() {
      const q = state.q.trim().toLowerCase();
      if (!q) { trace(null); return; }
      nodeEls.forEach(({ n, node }) => node.classList.toggle("g-dim", !(n.id.toLowerCase().includes(q) || (n.t && (n.t.title || "").toLowerCase().includes(q)))));
      edgeEls.forEach(({ line }) => line.classList.add("g-dim"));
    }

    function legend() {
      const box = document.getElementById("graph-legend");
      box.textContent = "";
      const item = (cls, text, style) => { const i = el("i", { class: cls }); if (style) i.style.background = style; box.append(el("span", {}, i, text)); };
      [["backlog", "--neu-mark"], ["open", "--series-1"], ["in-progress", "--series-7"], ["testing", "--series-2"], ["done", "--ok-mark"]]
        .forEach(([s, c]) => item("", LABEL[s], `var(${c})`));
      item("you", "Needs you");
      if (state.view !== "deps") { item("file", "File"); item("line collide", "Collision"); }
      item("line parent", "Epic");
      item("line blocks", "Blocks →");
    }
    function count() {
      const t = current.nodes.filter((n) => n.kind === "ticket").length, f = current.nodes.filter((n) => n.kind === "file").length;
      document.getElementById("graph-count").textContent = state.view === "deps"
        ? `${t} tickets · ${current.edges.filter((e) => e.kind === "blocks").length} blocking links`
        : `${t} tickets · ${f} files · ${current.nodes.filter((n) => collide.has(n.id)).length} with a collision`;
    }

    // ---------- pan, zoom, drag ----------
    let moved = false;
    function startDrag(ev, n) {
      if (ev.button !== 0) return;
      ev.stopPropagation();
      moved = false;
      const sx = ev.clientX, sy = ev.clientY, ox = n.x, oy = n.y;
      const move = (m) => {
        if (Math.abs(m.clientX - sx) + Math.abs(m.clientY - sy) > 3) moved = true;
        n.x = ox + (m.clientX - sx) / view.k; n.y = oy + (m.clientY - sy) / view.k;
        place();
      };
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    }
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      const sx = ev.clientX, sy = ev.clientY, ox = view.x, oy = view.y;
      svg.classList.add("panning");
      const move = (m) => { view.x = ox + m.clientX - sx; view.y = oy + m.clientY - sy; applyView(); };
      const up = () => { svg.classList.remove("panning"); window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    const zoomAt = (factor, cx, cy) => {
      const k = Math.max(0.2, Math.min(3, view.k * factor));
      view.x = cx - (cx - view.x) * (k / view.k); view.y = cy - (cy - view.y) * (k / view.k); view.k = k;
      applyView();
    };
    svg.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      zoomAt(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    fig.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
      const r = svg.getBoundingClientRect();
      if (b.dataset.zoom === "fit") fit(); else zoomAt(b.dataset.zoom === "in" ? 1.25 : 0.8, r.width / 2, r.height / 2);
    }));

    // ---------- inspector ----------
    const rows = (items) => el("ul", { class: "graph-rows" }, items.map(([id, title, meta]) => el("li", {},
      (() => { const b = el("button", { type: "button" }, el("span", { class: "id" }, tickets.has(id) ? id : collide.has(id) ? "⚠" : "▫"),
        el("span", { class: "t" }, title), el("span", { class: "m" }, meta)); b.addEventListener("click", () => select(id)); return b; })())));
    const section = (title, body) => [el("h3", { class: "graph-sub" }, title), body];
    let asked = 0;
    function agent(query, command) {
      const pre = el("pre", { class: "graph-agent" }, "Loading…");
      const mine = (asked += 1);
      fetch("/graph/related?" + query, { credentials: "same-origin" })
        .then((r) => r.json())
        .then((j) => { if (mine === asked) pre.textContent = j.text || j.error || ""; })
        .catch(() => { if (mine === asked) pre.textContent = `Could not load. Run ${command} instead.`; });
      return [el("h3", { class: "graph-sub" }, "What an agent reads"), el("p", { class: "muted" }, el("code", {}, command)), pre];
    }
    function inspect() {
      const id = state.sel;
      inspector.textContent = "";
      const t = tickets.get(id);
      if (t) {
        const changed = [...(tf.get(id) || new Map())].sort((a, b) => b[1] - a[1]);
        const others = new Map();
        changed.forEach(([f]) => (ft.get(f) || new Map()).forEach((n, k) => { if (k !== id) others.set(k, (others.get(k) || new Set()).add(f)); }));
        const open = [...others].filter(([k]) => isOpen(k));
        const done = [...others].filter(([k]) => !isOpen(k)).sort((a, b) => b[1].size - a[1].size);
        const linked = [];
        links.forEach((l) => {
          if (l.target === id) linked.push([l.source, l.kind === "blocks" ? "blocked by" : l.kind === "parent" ? "epic" : "follow-up of"]);
          if (l.source === id) linked.push([l.target, l.kind === "blocks" ? "blocks" : l.kind === "parent" ? "child" : "follow-up"]);
        });
        const role = ROLE[t.status] || "neu";
        inspector.append(
          el("div", { class: "graph-kicker" }, el("code", {}, id), chip(role, LABEL[t.status] || t.status), t.needs ? chip("you", t.needs) : null,
            t.type && t.type !== "feature" ? el("span", {}, t.type) : null),
          el("h2", { class: "card-title" }, t.title || id),
          t.summary ? el("p", { class: "muted" }, t.summary) : null,
          ...section("Changed files", changed.length ? rows(changed.map(([f, n]) => [f, f, `${n} commit${n === 1 ? "" : "s"}`]))
            : el("p", { class: "muted" }, `No commit names ${id} yet.`)),
          ...(open.length ? section("Open in the same files", rows(open.map(([k, fs]) => [k, tickets.get(k).title, [...fs].map(base).join(", ")]))) : []),
          ...(done.length ? section("Earlier work here", rows(done.slice(0, 8).map(([k, fs]) => [k, tickets.get(k).title, `${fs.size} file${fs.size === 1 ? "" : "s"}`]))) : []),
          ...(linked.length ? section("Linked", rows(linked.map(([k, rel]) => [k, tickets.get(k).title, rel]))) : []),
          el("div", { class: "graph-actions" }, el("a", { class: "btn btn-primary", href: "/t/" + encodeURIComponent(id) }, "Open ticket"),
            t.type === "epic" ? null : (() => { const b = el("button", { type: "button", class: "btn" }, `Around ${id}`); b.addEventListener("click", () => { state.focus = id; setView("local"); }); return b; })()),
          ...agent("t=" + encodeURIComponent(id), `orch related ${id}`));
      } else if (ft.has(id)) {
        const by = [...ft.get(id)].sort((a, b) => b[1] - a[1]);
        const openN = by.filter(([k]) => isOpen(k)).length;
        inspector.append(
          el("div", { class: "graph-kicker" }, el("span", {}, "File"), collide.has(id) ? chip("warn", `${openN} open tickets`) : null),
          el("h2", { class: "card-title" }, el("code", {}, id)),
          collide.has(id) ? el("p", { class: "muted" }, "More than one open ticket changes this file. Decide an order (blocked_by) before both reach testing.") : null,
          ...section("Tickets that changed it", rows(by.map(([k, n]) => [k, tickets.get(k).title, `${LABEL[tickets.get(k).status] || ""} · ${n}`]))),
          ...agent("p=" + encodeURIComponent(id), `orch related -p ${id}`));
      } else {
        inspector.append(el("h2", { class: "card-title" }, "Pick a node"),
          el("p", { class: "muted" }, "Click a ticket or a file to see what changed it, who else is in the same code and what an agent reads with orch related."));
      }
    }

    function select(id) {
      state.sel = id;
      if (tickets.has(id) && tickets.get(id).type !== "epic") { state.focus = id; focusLabel(); }
      if (!nodeEls.has(id)) { // picked from a list: show it
        state.done = true; state.epic = ""; state.only = false; sync();
        if (state.view === "local" && tickets.has(id)) state.focus = id;
        draw();
      }
      mark();
      inspect();
      url();
    }

    // ---------- controls ----------
    const tabs = [...document.querySelectorAll(".graph-views [data-view]")];
    const focusLabel = () => document.querySelectorAll("[data-graph-focus]").forEach((s) => { s.textContent = state.focus || "a ticket"; });
    function url() {
      const p = new URLSearchParams();
      if (state.view !== "code") p.set("view", state.view);
      if (state.sel && tickets.has(state.sel)) p.set("t", state.sel);
      try { history.replaceState(history.state, "", "/graph" + (p.toString() ? "?" + p : "")); } catch (e) { /* not allowed: fine */ }
      const local = tabs.find((a) => a.dataset.view === "local");
      if (local) local.setAttribute("href", "/graph?view=local" + (state.focus ? "&t=" + encodeURIComponent(state.focus) : ""));
    }
    function setView(v) {
      state.view = v;
      tabs.forEach((a) => { const on = a.dataset.view === v; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); });
      document.getElementById("graph-collide").disabled = v === "deps";
      focusLabel(); draw(); inspect(); url();
    }
    function sync() {
      document.getElementById("graph-done").checked = state.done;
      document.getElementById("graph-collide").checked = state.only;
      document.getElementById("graph-epic").value = state.epic;
    }
    tabs.forEach((a) => a.addEventListener("click", (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      setView(a.dataset.view);
    }));
    document.getElementById("graph-done").addEventListener("change", (ev) => { state.done = ev.target.checked; draw(); });
    document.getElementById("graph-collide").addEventListener("change", (ev) => { state.only = ev.target.checked; draw(); });
    document.getElementById("graph-epic").addEventListener("change", (ev) => { state.epic = ev.target.value; draw(); });
    const qbox = document.getElementById("graph-q");
    qbox.addEventListener("input", () => { state.q = qbox.value; search(); });
    qbox.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      const q = state.q.trim().toLowerCase();
      const hit = current.nodes.find((n) => n.kind !== "folder" && (n.id.toLowerCase().includes(q) || (n.t && (n.t.title || "").toLowerCase().includes(q))));
      if (hit) select(hit.id);
    });
    let resize;
    window.addEventListener("resize", () => { clearTimeout(resize); resize = setTimeout(fit, 200); });

    if (params.get("t") && !state.sel) state.sel = params.get("t");
    if (state.view === "local" && !state.sel) state.sel = state.focus;
    setView(state.view);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
