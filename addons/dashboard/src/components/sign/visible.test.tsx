import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { plain, prose, Raw, visible, visibleValue } from './visible'

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

// Security review #8: the encoding must be injective. Each pair below is two different values that used to share a display.
describe('visible / visibleValue: no two signed values look alike', () => {
  const PAIRS: [string, string][] = [
    ['​', '\\u{200b}'], // an invisible character vs the text of its escape
    ['', '""'], // empty vs two quote characters
    [' ', '␠'], // an edge space vs the marker character itself
    ['͏', ''], // COMBINING GRAPHEME JOINER (default-ignorable, not Cf)
    ['a͏b', 'ab'],
    ['a b', 'a b'], // no-break space vs space
    ['a　b', 'a b'], // ideographic space
    ['ㅤ', ''], // HANGUL FILLER (default-ignorable, a letter)
    ['a️b', 'ab'], // variation selector
    ['é', 'é'], // precomposed vs combining accent
    ['Å', 'Å'], // A with ring vs ANGSTROM SIGN (a canonical singleton)
    ['\uac00', '\u1100\u1161'], // 가 vs its decomposed jamo (letters, not marks; round 2 #3)
    ['\uac00x', '\u1100\u1161x'],
    ['\u00e9\uac00', 'e\u0301\u1100\u1161'],
    ['\\', '\\\\'],
    ['x\\', 'x\\\\'],
  ]
  it.each(PAIRS)('visible(%j) differs from visible(%j)', (a, b) => {
    expect(visible(a)).not.toBe(visible(b))
  })
  it('a string NFC would change is shown in ASCII only, so it cannot look like its normalized twin (round 2 #3)', () => {
    expect(visible('\u1100\u1161')).toBe('\\u{1100}\\u{1161}')
    expect(visible('e\u0301')).toBe('e\\u{301}')
    expect(visible('\u212b')).toBe('\\u{212b}')
    for (const s of ['\u1100\u1161x', 'Zu\u0308rich', '\u212b', 'e\u0301\uac00']) expect(visible(s), JSON.stringify(s)).toMatch(/^[\x20-\x7e]*$/)
    expect(visible('\uac00')).toBe('\uac00') // already NFC: readable
  })
  it('a typed value tells strings from numbers and booleans', () => {
    expect(visibleValue('1')).not.toBe(visibleValue(1))
    expect(visibleValue('true')).not.toBe(visibleValue(true))
    expect(visibleValue(0)).not.toBe(visibleValue(-0))
    expect(visibleValue('a"b')).not.toBe(visibleValue('a\\"b'))
    expect(visibleValue('')).toBe('""')
    expect(visibleValue('x')).toBe('"x"')
    expect(visibleValue(12)).toBe('12')
  })
  it('readable text stays readable: plain ASCII, letters of other scripts, and newlines in prose', () => {
    expect(visible('factory.permit:P-2')).toBe('factory.permit:P-2')
    expect(visible('psql -c "select 1"')).toBe('psql -c "select 1"')
    expect(visible('Zürich')).toBe('Zürich')
    expect(prose('line one\nline two')).toBe('line one\nline two')
    expect(prose('a‮b\n')).toBe('a\\u{202e}b\n')
  })
  it('escapes nothing it does not have to, and every escape decodes back (round trip)', () => {
    const decode = (v: string) => (v === '""' ? '' : v.replace(/^␠+|␠+$/g, (m) => ' '.repeat(m.length)).replace(/\\(\\|"|u\{([0-9a-f]+)\})/g, (_, c: string, hex?: string) => (hex ? String.fromCodePoint(parseInt(hex, 16)) : c)))
    for (const s of [...PAIRS.flat(), ' x ', 'a b', '""', '\\u{41}', 'ab‮cd']) expect(decode(visible(s)), JSON.stringify(s)).toBe(s)
  })
})
