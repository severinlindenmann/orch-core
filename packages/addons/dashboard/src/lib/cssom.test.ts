import { afterEach, expect, it, vi } from 'vitest'
import { dropCss, setCss } from './cssom'

// jsdom has no constructable stylesheets: a small stand-in records what the page adopts.
class FakeSheet {
  css = ''
  replaceSync(css: string) { this.css = css }
}
afterEach(() => { vi.unstubAllGlobals(); Reflect.deleteProperty(document, 'adoptedStyleSheets') })

it('adopts a stylesheet for an element, replaces it on update and drops it with dropCss', () => {
  vi.stubGlobal('CSSStyleSheet', FakeSheet)
  Object.defineProperty(document, 'adoptedStyleSheets', { value: [], writable: true, configurable: true })
  const el = document.createElement('span')
  document.head.appendChild(el)
  setCss(el, 'a { color: red }')
  const adopted = document.adoptedStyleSheets as unknown as FakeSheet[]
  expect(adopted.map((s) => s.css)).toEqual(['a { color: red }'])
  setCss(el, 'a { color: blue }')
  expect((document.adoptedStyleSheets as unknown as FakeSheet[]).map((s) => s.css)).toEqual(['a { color: blue }'])
  dropCss(el)
  expect(document.adoptedStyleSheets).toEqual([])
  el.remove()
})

it('drops the css of an element that left the page, at the next call', () => {
  vi.stubGlobal('CSSStyleSheet', FakeSheet)
  Object.defineProperty(document, 'adoptedStyleSheets', { value: [], writable: true, configurable: true })
  const a = document.createElement('span')
  const b = document.createElement('span')
  document.body.append(a, b)
  setCss(a, 'a {}')
  a.remove()
  setCss(b, 'b {}')
  expect((document.adoptedStyleSheets as unknown as FakeSheet[]).map((s) => s.css)).toEqual(['b {}'])
  dropCss(b)
  b.remove()
})

it('does nothing without constructable stylesheets, and never creates a <style> element', () => {
  const before = document.querySelectorAll('style').length
  const el = document.createElement('span')
  setCss(el, 'a {}')
  expect(document.querySelectorAll('style')).toHaveLength(before)
})
