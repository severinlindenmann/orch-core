import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { plain, Raw, visible } from './visible'

// Strings a malicious addon could send to make two values look alike or to hide or reorder text.
const TRICKY = ['ab\u202Ecd', 'ab\u200Bcd', 'abcd', 'a\n\nb', 'a b', ' x ', 'x', '', '\u2066evil\u2069']

describe('visible: what core shows for a string it did not write', () => {
  it('shows control and format characters (bidi override, zero-width, newlines) as escapes', () => {
    expect(visible('ab\u202Ecd')).toBe('ab\\u{202e}cd')
    expect(visible('ab\u200Bcd')).toBe('ab\\u{200b}cd')
    expect(visible('a\n\nb')).toBe('a\\u{a}\\u{a}b')
    expect(visible('\u2066evil\u2069')).toBe('\\u{2066}evil\\u{2069}')
  })
  it('shows edge spaces and the empty string', () => {
    expect(visible(' x ')).toBe('␠x␠')
    expect(visible('')).toBe('""')
    expect(visible('a b')).toBe('a b')
  })
  it('renders every tricky value distinguishably, in an isolating bdi', () => {
    const shown = TRICKY.map((t) => {
      const { container, unmount } = render(<Raw>{t}</Raw>)
      const el = container.querySelector('bdi')!
      expect(el.className).toMatch(/whitespace-pre-wrap/)
      const text = el.textContent!
      unmount()
      return text
    })
    expect(new Set(shown).size).toBe(TRICKY.length)
    for (const s of shown) expect(s).not.toMatch(/[\u200B\u202E\u2066\u2069\n]/)
  })
  it('plain() isolates anything beyond printable ASCII for text contexts and leaves plain ids alone', () => {
    expect(plain('schedules')).toBe('schedules')
    expect(plain('ab\u202Ecd')).toBe('ab\\u{202e}cd') // escaped: nothing left to reorder
    expect(plain('\u05E9\u05DC\u05D5\u05DD')).toBe('\u2068\u05E9\u05DC\u05D5\u05DD\u2069') // right-to-left letters are isolated
  })
})
