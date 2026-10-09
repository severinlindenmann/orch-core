// Widget templates the mock ships (v1 widgets.md, "Templates"): fixed HTML pages keyed by name@version that a ticket
// block names and pins. A page runs only inside the sandboxed `frame` node (sandbox="allow-scripts", CSP, no network),
// reads its data from `orch.data`, builds its DOM with createElement/textContent only and never navigates. Pure data,
// part of the API contract: the mock lists them (addon page), the ticket page draws them.
import { sha256Hex } from './sha256'
import { checkFlow, checkImageCompare, checkTableExplorer, FLOW, IMAGE_COMPARE, TABLE_EXPLORER } from './widgetTemplatesMore'

export interface WidgetTemplate {
  name: string
  version: number
  title: string
  description: string
  moment: 'understand' | 'decide' | 'plan' | 'verify' | 'review' | 'debug' | 'report'
  /** The frame's starting height in px. */
  minHeight: number
  libs: string[]
  /** The widget body: markup plus one script. */
  html: string
  /**
   * Core's strict check of a block's `data` before the frame gets it: a reason, or undefined when it fits. A template
   * without one takes any object (its page reads defensively). Not part of the pin, like a schema in widgets.md.
   */
  check?: (data: Record<string, unknown>) => string | undefined
}

const BEFORE_AFTER = `<style>
.stage{position:relative;border:1px solid GrayText;border-radius:6px;overflow:hidden}
.pane{margin:0;padding:10px 12px;min-height:110px;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap}
.pane h4{margin:0 0 6px;font:600 11px ui-sans-serif,system-ui,sans-serif;text-transform:uppercase;letter-spacing:.04em;color:GrayText}
.over{position:absolute;inset:0;background:Canvas;border-right:2px solid Highlight}
.row{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px}
.row input{flex:1}
</style>
<div class="stage"><div class="pane" id="after"></div><div class="pane over" id="before"></div></div>
<div class="row"><span id="lb"></span><input id="r" type="range" min="0" max="100" value="50" aria-label="Slide between before and after"><span id="la"></span></div>
<script>
(function () {
  var d = orch.data
  function fill(el, p) {
    var h = document.createElement('h4')
    h.textContent = p.title
    el.appendChild(h)
    el.appendChild(document.createTextNode((p.lines || []).join('\\n')))
  }
  fill(document.getElementById('before'), d.before)
  fill(document.getElementById('after'), d.after)
  document.getElementById('lb').textContent = d.before.title
  document.getElementById('la').textContent = d.after.title
  var over = document.getElementById('before')
  function set(v) { over.style.clipPath = 'inset(0 ' + (100 - v) + '% 0 0)' }
  var r = document.getElementById('r')
  r.addEventListener('input', function () { set(Number(r.value)) })
  set(50)
})()
</script>`

const LINE_CHART = `<style>
svg{display:block;width:100%;height:auto}
.axis{stroke:GrayText;stroke-width:1}
.line{fill:none;stroke:LinkText;stroke-width:2}
.dot{fill:LinkText}
text{font:10px ui-sans-serif,system-ui,sans-serif;fill:GrayText}
</style>
<svg id="c" viewBox="0 0 320 150" role="img"></svg>
<script>
(function () {
  var d = orch.data, ns = 'http://www.w3.org/2000/svg', svg = document.getElementById('c')
  var pts = d.points || [], unit = d.unit ? ' ' + d.unit : ''
  function el(n, a, t) {
    var e = document.createElementNS(ns, n)
    for (var k in a) e.setAttribute(k, a[k])
    if (t != null) e.textContent = t
    return e
  }
  var xs = pts.map(function (p) { return p[0] }), ys = pts.map(function (p) { return p[1] })
  var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y1 = Math.max.apply(null, ys), y0 = Math.min(0, Math.min.apply(null, ys))
  function X(v) { return 34 + (x1 === x0 ? 0 : (v - x0) / (x1 - x0)) * 272 }
  function Y(v) { return 126 - (y1 === y0 ? 0 : (v - y0) / (y1 - y0)) * 106 }
  svg.appendChild(el('title', {}, (d.title || 'Line chart') + ': ' + pts.map(function (p) { return p[0] + ' ' + p[1] + unit }).join(', ')))
  svg.appendChild(el('line', { 'class': 'axis', x1: 34, y1: 126, x2: 306, y2: 126 }))
  svg.appendChild(el('line', { 'class': 'axis', x1: 34, y1: 20, x2: 34, y2: 126 }))
  svg.appendChild(el('text', { x: 30, y: 24, 'text-anchor': 'end' }, String(y1)))
  svg.appendChild(el('text', { x: 30, y: 126, 'text-anchor': 'end' }, String(y0)))
  svg.appendChild(el('text', { x: 34, y: 142 }, String(x0)))
  svg.appendChild(el('text', { x: 306, y: 142, 'text-anchor': 'end' }, String(x1)))
  svg.appendChild(el('polyline', { 'class': 'line', points: pts.map(function (p) { return X(p[0]) + ',' + Y(p[1]) }).join(' ') }))
  pts.forEach(function (p) {
    var c = el('circle', { 'class': 'dot', cx: X(p[0]), cy: Y(p[1]), r: 3 })
    c.appendChild(el('title', {}, p[0] + ': ' + p[1] + unit))
    svg.appendChild(c)
  })
})()
</script>`

const OPTION_PROTOTYPE = `<style>
.opts{display:grid;gap:6px}
label.o{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;border:1px solid GrayText;border-radius:6px;padding:7px 9px;cursor:pointer}
label.o:has(input:checked){border-color:Highlight;outline:1px solid Highlight}
.meta{font-size:11px;color:GrayText}
.rec{font-size:10px;border:1px solid Highlight;border-radius:99px;padding:0 6px}
#out{margin-top:8px;padding:8px 10px;border-left:3px solid Highlight;font-size:12px}
</style>
<div class="opts" id="opts" role="radiogroup" aria-label="Options"></div>
<div id="out" aria-live="polite"></div>
<script>
(function () {
  var d = orch.data, box = document.getElementById('opts'), out = document.getElementById('out')
  function show(o) {
    out.textContent = ''
    var b = document.createElement('strong')
    b.textContent = o.title + ': '
    out.appendChild(b)
    out.appendChild(document.createTextNode(o.notes || 'No notes.'))
  }
  ;(d.options || []).forEach(function (o) {
    var l = document.createElement('label'), i = document.createElement('input'), t = document.createElement('span'), m = document.createElement('span')
    l.className = 'o'
    i.type = 'radio'
    i.name = 'opt'
    i.value = o.id
    t.textContent = o.title
    m.className = 'meta'
    m.textContent = [o.cost && 'cost ' + o.cost, o.risk && 'risk ' + o.risk].filter(Boolean).join(' · ')
    l.appendChild(i)
    l.appendChild(t)
    l.appendChild(m)
    if (d.pick === o.id) {
      var r = document.createElement('span')
      r.className = 'rec'
      r.textContent = 'recommended'
      t.appendChild(document.createTextNode(' '))
      t.appendChild(r)
    }
    i.addEventListener('change', function () { show(o) })
    box.appendChild(l)
    if (d.pick === o.id) { i.checked = true; show(o) }
  })
})()
</script>`

export const TEMPLATES: WidgetTemplate[] = [
  { name: 'before-after', version: 1, title: 'Before / after slider', description: 'Two versions of the same text, revealed with a slider.', moment: 'review', minHeight: 200, libs: [], html: BEFORE_AFTER },
  { name: 'line-chart', version: 1, title: 'Line chart', description: 'A small line over x with a value label per point.', moment: 'understand', minHeight: 190, libs: [], html: LINE_CHART },
  { name: 'option-prototype', version: 1, title: 'Option prototype', description: 'Options with cost and risk that the reader can click through.', moment: 'decide', minHeight: 230, libs: [], html: OPTION_PROTOTYPE },
  { name: 'image-compare', version: 1, title: 'Image compare', description: 'Two screenshots, a slider or side by side. Inline data: images only.', moment: 'review', minHeight: 240, libs: [], html: IMAGE_COMPARE, check: checkImageCompare },
  { name: 'flow-diagram', version: 1, title: 'Flow diagram', description: 'Boxes and arrows from a list of steps and links, left to right.', moment: 'understand', minHeight: 160, libs: [], html: FLOW, check: checkFlow },
  { name: 'table-explorer', version: 1, title: 'Table explorer', description: 'A table the reader can sort and filter.', moment: 'debug', minHeight: 240, libs: [], html: TABLE_EXPLORER, check: checkTableExplorer },
]

/** The pin: sha256 of the page, a NUL byte and the sorted libs as compact JSON (v1 widgets.md, "Pinned templates"). */
export function templateDigest(t: Pick<WidgetTemplate, 'html' | 'libs'>): string {
  return sha256Hex(`${t.html}\0${JSON.stringify([...t.libs].sort())}`)
}

export function findTemplate(ref: string): WidgetTemplate | undefined {
  const m = /^([a-z][a-z0-9-]*)@(\d+)$/.exec(ref)
  return m ? TEMPLATES.find((t) => t.name === m[1] && t.version === Number(m[2])) : undefined
}

/**
 * The frame document for a template or a one-off page: the block's data as inert JSON (`<` escaped), a tiny `orch`
 * object (data only; the text/ready/resize hooks are no-ops in the mock), then the page. The frame node prepends the CSP.
 * Core's base style keeps an SVG inside the frame's height (a width-100% chart in a wide card would otherwise be cut
 * off); it is core's wrapper, not part of any template's pinned bytes.
 */
export function frameDocument(page: string, data: unknown): string {
  const json = JSON.stringify(data ?? {}).replace(/</g, '\\u003c').replace(/[\u2028\u2029]/g, (c) => '\\u' + c.charCodeAt(0).toString(16))
  return (
    '<style>:root{color-scheme:dark}html,body{margin:0}body{padding:8px;font:12px/1.45 ui-sans-serif,system-ui,sans-serif;color:CanvasText;background:Canvas}svg{max-height:calc(100vh - 16px)}</style>' +
    `<script type="application/json" id="orch-data">${json}</script>` +
    '<script>window.orch={data:JSON.parse(document.getElementById("orch-data").textContent),text:function(){},ready:function(){},resize:function(){}}</script>' +
    page
  )
}
