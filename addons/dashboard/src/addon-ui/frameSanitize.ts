// Core's sanitizer for HTML it did not write (an addon's frame node, an agent's HTML artifact or one-off widget page).
// Such HTML is drawn INERT (security review #1): `sandbox=""` (no scripts, forms, popups, top navigation), core's CSP
// (no fetches or subresources except inline styles and data: images), and this sanitizer first, which takes out every
// element and attribute that could navigate the frame or load something: a frame's own navigation is not governed by
// CSP, and removing the frame after a second load would come after the request was made. Nothing here relies on the
// frame being removed afterwards.

/** Elements dropped with their content. */
const DROP = new Set([
  'script', 'meta', 'base', 'link', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'portal', 'fencedframe',
  'template', 'noembed', 'noframes', 'param', 'source', 'track', 'audio', 'video', 'picture',
  // SVG animation can rewrite an href after load; SVG/MathML script and foreign content are not needed for a static page.
  'set', 'animate', 'animatemotion', 'animatetransform', 'discard', 'handler', 'listener', 'foreignobject',
])
/** Elements replaced by their children (the fields stay, the navigation does not). */
const UNWRAP = new Set(['form'])
/** Attributes that name a URL to load or navigate to. A `href` is kept only as an in-page anchor (`#…`); a `src` only as data:image. */
const URL_ATTRS = new Set(['href', 'xlink:href', 'src', 'srcset', 'action', 'formaction', 'ping', 'background', 'poster', 'data', 'codebase', 'cite', 'longdesc', 'lowsrc', 'dynsrc', 'manifest', 'archive', 'classid', 'usemap', 'imagesrcset'])
/** Attributes dropped whatever their value. */
const DROP_ATTRS = new Set(['target', 'formtarget', 'download', 'http-equiv'])

const isAnchor = (v: string) => /^#[^\s]*$/.test(v.trim())
const isDataImage = (v: string) => /^data:image\/(png|gif|jpe?g|webp|bmp|svg\+xml)[;,]/i.test(v.trim())

function cleanOnce(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  for (const el of [...doc.querySelectorAll('*')]) {
    if (!el.isConnected) continue
    const name = el.localName.toLowerCase()
    if (DROP.has(name)) {
      el.remove()
      continue
    }
    for (const a of [...el.attributes]) {
      const n = a.name.toLowerCase()
      const keep =
        !n.startsWith('on') &&
        !DROP_ATTRS.has(n) &&
        (!URL_ATTRS.has(n) || ((n === 'href' || n === 'xlink:href') && isAnchor(a.value)) || (n === 'src' && name === 'img' && isDataImage(a.value))) &&
        !/javascript:|vbscript:/i.test(a.value) &&
        // A style may not reach out either (CSP blocks it too): no url() other than data: images, no @import.
        !(n === 'style' && /url\s*\(\s*['"]?(?!data:image\/)|@import/i.test(a.value))
      if (!keep) el.removeAttribute(a.name)
    }
    if (UNWRAP.has(name)) el.replaceWith(...el.childNodes)
  }
  const head = [...doc.head.children].filter((e) => e.localName === 'style').map((e) => e.outerHTML).join('')
  return head + doc.body.innerHTML
}

/**
 * The HTML without anything that could navigate the frame or load a resource (see the lists above). Run until it no
 * longer changes, so markup that re-parses differently (mutation tricks) is cleaned again; if it does not settle, the
 * result is empty (fail closed).
 */
export function sanitizeFrameHtml(html: string): string {
  let cur = html
  for (let i = 0; i < 4; i++) {
    const next = cleanOnce(cur)
    if (next === cur) return next
    cur = next
  }
  return cleanOnce(cur) === cur ? cur : ''
}

/** Core's CSP for an inert frame: no scripts at all, no fetches, no forms, no base; inline styles and data: images only. */
export const INERT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'"

/** The document core gives an inert frame: its CSP first, then the sanitized HTML. */
export function inertDocument(html: string): string {
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${INERT_CSP}"></head><body>${sanitizeFrameHtml(html)}</body></html>`
}

/** Does this HTML rely on scripts (script elements, event handlers, javascript: URLs)? An inert frame would drop them. */
export function usesScripts(html: string): boolean {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  for (const el of doc.querySelectorAll('*')) {
    if (el.localName.toLowerCase() === 'script') return true
    for (const a of el.attributes) if (a.name.toLowerCase().startsWith('on') || /^\s*javascript:/i.test(a.value)) return true
  }
  return false
}
