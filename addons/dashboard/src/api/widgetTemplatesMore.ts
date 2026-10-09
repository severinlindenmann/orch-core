// The catalog's newer templates (wave 3): image-compare, flow and table-explorer. Same rules as the first three
// (widgetTemplates.ts): the page runs only in the sandboxed frame, reads `orch.data`, builds its DOM with
// createElement/textContent only, never navigates and loads nothing. Each also has a strict data check that core runs
// BEFORE the frame is drawn (fail closed: data that does not fit is refused as code with a reason, the frame never gets it).
// The checks are display-side like a template's schema in widgets.md: they are not part of the pin.

import { has, isObj, unknownKey as extra, type Obj } from './strictObject'

const str = (v: unknown, min: number, max: number) => typeof v === 'string' && v.length >= min && v.length <= max

/** Only inline raster images: no URL of any kind, no SVG (it is a document, not a picture). */
export const DATA_IMAGE_RE = /^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$/

export function checkImageCompare(d: Obj): string | undefined {
  const e = extra(d, ['before', 'after', 'alt'], 'data')
  if (e) return e
  for (const side of ['before', 'after'] as const) {
    const s = d[side]
    if (!isObj(s)) return `${side} must be an object with src`
    const se = extra(s, ['src', 'label'], side)
    if (se) return se
    if (typeof s.src !== 'string' || !DATA_IMAGE_RE.test(s.src)) return `${side}.src must be a data:image/png, jpeg, gif or webp URI (no links, no SVG)`
    if (has(s, 'label') && !str(s.label, 1, 80)) return `${side}.label must be 1 to 80 characters`
  }
  if (has(d, 'alt') && !str(d.alt, 1, 200)) return 'alt must be 1 to 200 characters'
}

const FLOW_ID = /^[a-z0-9][a-z0-9-]{0,39}$/
export function checkFlow(d: Obj): string | undefined {
  const e = extra(d, ['nodes', 'edges'], 'data')
  if (e) return e
  if (!Array.isArray(d.nodes) || d.nodes.length < 1 || d.nodes.length > 30) return 'nodes needs 1 to 30 boxes'
  const ids = new Set<string>()
  for (const n of d.nodes) {
    if (!isObj(n)) return 'each node must be an object'
    const ne = extra(n, ['id', 'label', 'status'], 'a node')
    if (ne) return ne
    if (typeof n.id !== 'string' || !FLOW_ID.test(n.id)) return 'a node id must match [a-z0-9][a-z0-9-]{0,39}'
    if (ids.has(n.id)) return `node id "${n.id}" is used twice`
    ids.add(n.id)
    if (!str(n.label, 1, 80)) return 'a node label must be 1 to 80 characters'
    if (has(n, 'status') && !['done', 'current', 'next', 'blocked'].includes(n.status as string)) return 'a node status must be done, current, next or blocked'
  }
  const edges = has(d, 'edges') ? d.edges : []
  if (!Array.isArray(edges) || edges.length > 60) return 'edges must be a list of at most 60 arrows'
  const out = new Map<string, string[]>([...ids].map((i) => [i, []]))
  for (const x of edges) {
    if (!isObj(x)) return 'each edge must be an object'
    const xe = extra(x, ['from', 'to', 'label'], 'an edge')
    if (xe) return xe
    if (typeof x.from !== 'string' || !ids.has(x.from) || typeof x.to !== 'string' || !ids.has(x.to)) return 'an edge must join two node ids'
    if (x.from === x.to) return 'an edge must join two different nodes'
    if (has(x, 'label') && !str(x.label, 1, 40)) return 'an edge label must be 1 to 40 characters'
    out.get(x.from)!.push(x.to)
  }
  // Left-to-right layout by depth needs no loops.
  const state = new Map<string, number>()
  const loops = (id: string): boolean => {
    if (state.get(id) === 1) return true
    if (state.get(id) === 2) return false
    state.set(id, 1)
    const r = out.get(id)!.some(loops)
    state.set(id, 2)
    return r
  }
  if ([...ids].some(loops)) return 'edges form a loop; a flow runs one way'
}

export function checkTableExplorer(d: Obj): string | undefined {
  const e = extra(d, ['columns', 'rows'], 'data')
  if (e) return e
  if (!Array.isArray(d.columns) || d.columns.length < 1 || d.columns.length > 12 || !d.columns.every((c) => str(c, 1, 80))) return 'columns needs 1 to 12 names of at most 80 characters'
  if (!Array.isArray(d.rows) || d.rows.length > 500) return 'rows must be a list of at most 500 rows'
  const w = d.columns.length
  for (let i = 0; i < d.rows.length; i++) {
    const r = d.rows[i]
    if (!Array.isArray(r) || r.length !== w) return `row ${i + 1} must have ${w} cells`
    if (!r.every((c) => c === null || typeof c === 'number' || typeof c === 'boolean' || str(c, 0, 200))) return `a cell in row ${i + 1} must be a short string, number, boolean or null`
  }
}

export const IMAGE_COMPARE = `<style>
.stage{position:relative;max-width:360px;border:1px solid GrayText;border-radius:6px;overflow:hidden;line-height:0}
.stage img{display:block;width:100%;height:auto}
.stage .top{position:absolute;inset:0;border-right:2px solid Highlight}
.stage .top img{width:100%;height:100%;object-fit:fill}
.side{display:grid;grid-template-columns:1fr 1fr;gap:8px;max-width:640px}
.side figure{margin:0}
.side img{display:block;width:100%;border:1px solid GrayText;border-radius:6px}
figcaption,.row{font-size:12px;color:GrayText}
.row{display:flex;align-items:center;gap:8px;margin-top:8px;max-width:360px}
.row input{flex:1}
button{font:inherit;font-size:11px;color:CanvasText;background:Canvas;border:1px solid GrayText;border-radius:4px;padding:2px 8px;cursor:pointer}
.err{padding:8px;border:1px dashed GrayText;border-radius:6px}
</style>
<div id="root"></div>
<script>
(function () {
  var d = orch.data, root = document.getElementById('root')
  var OK = ['data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/gif;base64,', 'data:image/webp;base64,']
  function allowed(s) { return typeof s === 'string' && OK.some(function (p) { return s.indexOf(p) === 0 }) }
  function el(n, cls, text) { var e = document.createElement(n); if (cls) e.className = cls; if (text != null) e.textContent = text; return e }
  var b = d.before || {}, a = d.after || {}
  if (!allowed(b.src) || !allowed(a.src)) { root.appendChild(el('p', 'err', 'Image not shown: only inline data: images are allowed.')); return }
  var lb = b.label || 'Before', la = a.label || 'After'
  function img(p, label) { var i = el('img'); i.alt = (d.alt ? d.alt + ' ' : '') + '(' + label + ')'; i.src = p.src; return i }
  var mode = 'slider'
  function draw() {
    root.textContent = ''
    if (mode === 'slider') {
      var stage = el('div', 'stage'), top = el('div', 'top')
      stage.appendChild(img(a, la))
      top.appendChild(img(b, lb))
      stage.appendChild(top)
      root.appendChild(stage)
      var row = el('div', 'row'), r = el('input')
      r.type = 'range'; r.min = '0'; r.max = '100'; r.value = '50'
      r.setAttribute('aria-label', 'Slide between ' + lb + ' and ' + la)
      function set(v) { top.style.clipPath = 'inset(0 ' + (100 - v) + '% 0 0)' }
      r.addEventListener('input', function () { set(Number(r.value)) })
      set(50)
      row.appendChild(el('span', '', lb)); row.appendChild(r); row.appendChild(el('span', '', la)); row.appendChild(toggle('Side by side'))
      root.appendChild(row)
    } else {
      var side = el('div', 'side')
      ;[[b, lb], [a, la]].forEach(function (p) { var f = el('figure'); f.appendChild(img(p[0], p[1])); f.appendChild(el('figcaption', '', p[1])); side.appendChild(f) })
      root.appendChild(side)
      var row2 = el('div', 'row'); row2.appendChild(toggle('Slider')); root.appendChild(row2)
    }
  }
  function toggle(label) { var t = el('button', '', label); t.type = 'button'; t.addEventListener('click', function () { mode = mode === 'slider' ? 'side' : 'slider'; draw() }); return t }
  draw()
})()
</script>`

export const FLOW = `<style>
svg{display:block;width:100%;height:auto}
.box{fill:Canvas;stroke:GrayText;stroke-width:1}
.box.done{stroke:CanvasText}
.box.current{stroke:Highlight;stroke-width:2.5}
.box.blocked{stroke-dasharray:4 3}
.lbl{font:600 11px ui-sans-serif,system-ui,sans-serif;fill:CanvasText}
.st{font:10px ui-sans-serif,system-ui,sans-serif;fill:GrayText}
.edge{fill:none;stroke:GrayText;stroke-width:1.2}
.el{font:10px ui-sans-serif,system-ui,sans-serif;fill:GrayText;paint-order:stroke;stroke:Canvas;stroke-width:3px;stroke-linejoin:round}
.head{fill:GrayText}
</style>
<svg id="c" role="img"></svg>
<script>
(function () {
  var d = orch.data, ns = 'http://www.w3.org/2000/svg', svg = document.getElementById('c')
  var nodes = d.nodes || [], edges = d.edges || []
  var W = 132, H = 42, GX = 84, GY = 14, P = 8
  var WORD = { done: 'done', current: 'now', next: 'next', blocked: 'blocked' }
  function el(n, a, t) { var e = document.createElementNS(ns, n); for (var k in a) e.setAttribute(k, a[k]); if (t != null) e.textContent = t; return e }
  var depth = {}, indeg = {}, byId = {}
  nodes.forEach(function (n) { depth[n.id] = 0; indeg[n.id] = 0; byId[n.id] = n })
  edges.forEach(function (e) { indeg[e.to]++ })
  var queue = nodes.filter(function (n) { return indeg[n.id] === 0 }).map(function (n) { return n.id })
  while (queue.length) {
    var u = queue.shift()
    edges.forEach(function (e) {
      if (e.from !== u) return
      depth[e.to] = Math.max(depth[e.to], depth[u] + 1)
      if (--indeg[e.to] === 0) queue.push(e.to)
    })
  }
  var cols = [], pos = {}
  nodes.forEach(function (n) { var c = depth[n.id]; (cols[c] = cols[c] || []).push(n.id) })
  var rows = Math.max.apply(null, cols.map(function (c) { return c.length }))
  cols.forEach(function (c, ci) { c.forEach(function (id, ri) { pos[id] = { x: P + ci * (W + GX), y: P + ri * (H + GY) + ((rows - c.length) * (H + GY)) / 2 } }) })
  var vw = P * 2 + cols.length * (W + GX) - GX, vh = P * 2 + rows * (H + GY) - GY
  svg.setAttribute('viewBox', '0 0 ' + vw + ' ' + vh)
  svg.style.maxWidth = vw * 1.4 + 'px'
  svg.appendChild(el('title', {}, 'Flow: ' + edges.map(function (e) { return byId[e.from].label + ' to ' + byId[e.to].label + (e.label ? ' (' + e.label + ')' : '') }).join(', ')))
  edges.forEach(function (e) {
    var a = pos[e.from], b = pos[e.to], x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x - 6, y2 = b.y + H / 2, mx = (x1 + x2) / 2
    svg.appendChild(el('path', { 'class': 'edge', d: 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2 }))
    svg.appendChild(el('path', { 'class': 'head', d: 'M' + x2 + ',' + (y2 - 4) + ' L' + (x2 + 6) + ',' + y2 + ' L' + x2 + ',' + (y2 + 4) + ' z' }))
    if (e.label) svg.appendChild(el('text', { 'class': 'el', x: mx, y: (y1 + y2) / 2 - 4, 'text-anchor': 'middle' }, e.label))
  })
  nodes.forEach(function (n) {
    var p = pos[n.id], g = el('g', {})
    g.appendChild(el('rect', { 'class': 'box ' + (n.status || ''), x: p.x, y: p.y, width: W, height: H, rx: 6 }))
    var label = n.label.length > 20 ? n.label.slice(0, 19) + '\\u2026' : n.label
    g.appendChild(el('text', { 'class': 'lbl', x: p.x + 8, y: p.y + (n.status ? 17 : 25) }, label))
    if (n.status) g.appendChild(el('text', { 'class': 'st', x: p.x + 8, y: p.y + 32 }, WORD[n.status] || ''))
    g.appendChild(el('title', {}, n.label + (n.status ? ' (' + WORD[n.status] + ')' : '')))
    svg.appendChild(g)
  })
})()
</script>`

export const TABLE_EXPLORER = `<style>
.bar{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px;color:GrayText}
.bar input{flex:1;font:inherit;color:CanvasText;background:Canvas;border:1px solid GrayText;border-radius:4px;padding:3px 6px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{border-bottom:1px solid GrayText;padding:3px 8px;text-align:left}
th button{font:inherit;font-weight:600;color:CanvasText;background:none;border:0;padding:0;cursor:pointer}
td.num{text-align:right;font-variant-numeric:tabular-nums}
</style>
<div class="bar"><input id="q" type="search" placeholder="Filter rows" aria-label="Filter rows"><span id="n" aria-live="polite"></span></div>
<table><thead><tr id="h"></tr></thead><tbody id="b"></tbody></table>
<script>
(function () {
  var d = orch.data, cols = d.columns || [], rows = d.rows || []
  var sortCol = -1, dir = 1, q = document.getElementById('q'), head = document.getElementById('h'), body = document.getElementById('b'), count = document.getElementById('n')
  function text(c) { return c === null || c === undefined ? '' : typeof c === 'boolean' ? (c ? 'Yes' : 'No') : String(c) }
  function cmp(a, b) { if (typeof a === 'number' && typeof b === 'number') return a - b; return text(a).localeCompare(text(b)) }
  function draw() {
    head.textContent = ''
    cols.forEach(function (c, i) {
      var th = document.createElement('th'), b = document.createElement('button')
      th.setAttribute('aria-sort', sortCol === i ? (dir > 0 ? 'ascending' : 'descending') : 'none')
      b.type = 'button'
      b.textContent = c + (sortCol === i ? (dir > 0 ? ' \\u25B2' : ' \\u25BC') : '')
      b.addEventListener('click', function () { if (sortCol === i) dir = -dir; else { sortCol = i; dir = 1 } draw() })
      th.appendChild(b)
      head.appendChild(th)
    })
    var f = q.value.toLowerCase()
    var shown = rows.filter(function (r) { return !f || r.some(function (c) { return text(c).toLowerCase().indexOf(f) >= 0 }) })
    if (sortCol >= 0) shown = shown.slice().sort(function (a, b) { return cmp(a[sortCol], b[sortCol]) * dir })
    body.textContent = ''
    shown.forEach(function (r) {
      var tr = document.createElement('tr')
      r.forEach(function (c) { var td = document.createElement('td'); if (typeof c === 'number') td.className = 'num'; td.textContent = text(c); tr.appendChild(td) })
      body.appendChild(tr)
    })
    count.textContent = shown.length + ' of ' + rows.length + ' rows'
  }
  q.addEventListener('input', draw)
  draw()
})()
</script>`
