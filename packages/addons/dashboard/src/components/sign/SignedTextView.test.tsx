import { render } from '@testing-library/react'
import { expect, it } from 'vitest'
import { classifyLines, SignedTextView } from './SignedTextView'
import { prose } from './visible'

// Invariant (Opus challenge, 6.7/6.9): for every line, the rendered text equals the line minus its stripped leading marker.
const FIXTURE = [
  '# What happens when you sign',
  '## Context',
  '- File: elefanten.json',
  '* second bullet with **stars** and _underscores_',
  '1. Parse the input',
  '12. Twelve',
  '',
  'Original: "mit json"',
  'Q1 answered: separate',
  '<!-- hidden comment -->',
  '[click](http://evil.example/x) and [a](http://b)',
  '**bold?**',
  '  - indented dash stays a plain line',
  '-no space after dash',
  '#nospace',
  'rtl ‮evil‬ and zero​width and nbsp here',
  '1) not a number',
  '- ',
]
const TEXT = FIXTURE.join('\n')

it('every rendered line is the signed line minus its stripped leading marker, nothing merged, dropped or reordered', () => {
  const { container } = render(<SignedTextView text={TEXT} />)
  const rows = [...container.querySelectorAll('[data-line]')]
  expect(rows).toHaveLength(FIXTURE.length)
  const lines = classifyLines(TEXT)
  rows.forEach((row, i) => {
    expect(row.getAttribute('data-line')).toBe(lines[i].kind)
    const rendered = row.querySelector('[data-text]')?.textContent ?? ''
    expect(rendered, `line ${i}`).toBe(prose(lines[i].text))
  })
  // markers stripped: only those, and only at column 0
  expect(lines.map((l) => l.text)).toEqual([
    'What happens when you sign', 'Context', 'File: elefanten.json', 'second bullet with **stars** and _underscores_', 'Parse the input', 'Twelve', '',
    'Original: "mit json"', 'Q1 answered: separate', '<!-- hidden comment -->', '[click](http://evil.example/x) and [a](http://b)', '**bold?**',
    '  - indented dash stays a plain line', '-no space after dash', '#nospace', 'rtl ‮evil‬ and zero​width and nbsp here', '1) not a number', '',
  ])
})

it('draws no links, images, markup or comments from the text, and shows hidden characters', () => {
  const { container } = render(<SignedTextView text={TEXT} />)
  expect(container.querySelector('a, img, strong, em, iframe, script')).toBeNull()
  expect(container.textContent).toContain('<!-- hidden comment -->')
  expect(container.textContent).toContain('http://evil.example/x')
  expect(container.textContent).toContain('\\u{202e}')
  expect(container.textContent).toContain('\\u{200b}')
})

it('an agent heading is body-size bold text, and a fake "What happens when you sign" stays a line inside the view', () => {
  const { container } = render(<SignedTextView text="# What happens when you sign" />)
  const row = container.querySelector('[data-line="heading"]')!
  expect(row.className).toContain('font-semibold')
  expect(container.querySelector('h1, h2, h3, h4')).toBeNull()
})

it('a long list number grows the marker instead of being clipped (no fixed width)', () => {
  const { container } = render(<SignedTextView text={'1234567. Long number'} />)
  const m = container.querySelector('[data-marker]')!
  expect(m.className).toMatch(/\bmin-w-5\b/)
  expect(m.className).toMatch(/\bh-5\b/)
  expect(m.className.split(' ')).not.toContain('size-5')
  expect(m.className.split(' ')).not.toContain('w-5')
})
