// Styles that libraries would put into a <style> element go through constructable stylesheets instead: the server's
// CSP (`style-src 'self'`) blocks every inline <style> element, even an empty one, but not `adoptedStyleSheets`
// (B11 34). vite.config.ts rewrites react-style-singleton (the scroll lock of every modal dialog) and xterm's DOM
// renderer to call these two functions through `globalThis`; nothing else should need them.
const sheets = new Map<Element, { sheet: CSSStyleSheet; connected: boolean }>()

function drop(el: Element) {
  const got = sheets.get(el)
  if (!got) return
  sheets.delete(el)
  document.adoptedStyleSheets = document.adoptedStyleSheets.filter((s) => s !== got.sheet)
}

/** Sets the css that `el` stands for (replacing what it set before). An element removed from the page loses its css. */
export function setCss(el: Element, css: string) {
  if (typeof CSSStyleSheet !== 'function' || !('adoptedStyleSheets' in document)) return
  for (const [other, got] of sheets) {
    if (other.isConnected) got.connected = true
    else if (got.connected) drop(other)
  }
  let got = sheets.get(el)
  if (!got) {
    got = { sheet: new CSSStyleSheet(), connected: false }
    sheets.set(el, got)
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, got.sheet]
  }
  if (el.isConnected) got.connected = true
  got.sheet.replaceSync(css)
}

/** Removes the css `el` stood for. */
export function dropCss(el: Element) {
  drop(el)
}

const g = globalThis as unknown as { __orchSetCss?: typeof setCss; __orchDropCss?: typeof dropCss }
g.__orchSetCss = setCss
g.__orchDropCss = dropCss
