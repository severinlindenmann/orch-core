import { expect, it } from 'vitest'
import { STAGGER_SELECTOR } from './pageMotion'

it('the page stagger picks rows, cards and list items, never table rows', () => {
  document.body.innerHTML = '<table><tbody><tr id="t"><td>x</td></tr></tbody></table><div data-row id="d"></div><article id="a"></article><ul aria-label="L"><li id="l"></li></ul>'
  expect([...document.querySelectorAll(STAGGER_SELECTOR)].map((e) => e.id)).toEqual(['d', 'a', 'l'])
})
