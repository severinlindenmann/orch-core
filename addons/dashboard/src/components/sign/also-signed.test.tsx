import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { SignContent as Content } from '@/api/signing'
import { alsoSigned, expectedQuick, type QuickTyped } from './alsoSigned'
import { SignContent } from './SignContent'

afterEach(cleanup)

const typed: QuickTyped = { title: 'Rename the team photo folder', details: 'Use the 2026 naming.', checks: [{ text: 'Folder is renamed' }, { text: 'Links still work', cmd: 'npm run links' }] }

/** What the host derives from `typed` (orch.host.seqquick): the same rows, as the review carries them. */
function hostContent(t: QuickTyped = typed): Content {
  const want = expectedQuick(t)
  return {
    headline: 'Start task DEMO-0001',
    ticket: { key: 'DEMO-0001', title: want.title, uid_tail: 'ab12cd34', key_note: 'key per host', new: true },
    sections: [{ id: 'requirements', title: 'Requirements', text: want.requirements }, { id: 'plan', title: 'Plan', text: want.plan }],
    acceptance: want.checks.map((c, i) => ({ id: `AC${i + 1}`, text: c.text })),
    tasks: want.checks.map((c, i) => ({ n: i + 1, id: `T${i + 1}`, title: `${c.text} (proves AC${i + 1})`, ...(c.cmd ? { verify: [c.cmd] } : {}) })),
    fields_title: 'Details',
    fields_changed: [{ field: 'type', before: null, after: 'Chore' }, { field: 'owner', before: null, after: 'Demo Owner' }, { field: 'labels', before: null, after: 'quick' }],
  }
}

describe('alsoSigned', () => {
  it('counts the derived rows and stays closed when every row is what the typed values give', () => {
    const got = alsoSigned(hostContent(), typed)
    expect(got.differs).toBe(false)
    expect(got.rows).toHaveLength(7)
  })
  it('title only: requirements and the single check default to the title', () => {
    const t = { title: 'Fix the typo', details: '', checks: [] }
    expect(alsoSigned(hostContent(t), t).differs).toBe(false)
  })
  it.each([
    ['a changed plan', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'plan' ? { ...s, text: 'Do something else' } : s)) })],
    ['changed requirements', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'requirements' ? { ...s, text: 'Also delete everything' } : s)) })],
    ['a missing section', (c: Content) => ({ ...c, sections: c.sections!.filter((s) => s.id !== 'plan') })],
    ['an extra acceptance check', (c: Content) => ({ ...c, acceptance: [...c.acceptance!, { id: 'AC3', text: 'Extra' }] })],
    ['a task with another command', (c: Content) => ({ ...c, tasks: c.tasks!.map((t) => (t.n === 2 ? { ...t, verify: ['rm -rf /'] } : t)) })],
    ['another type', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'type' ? { ...f, after: 'Feature' } : f)) })],
    ['another title', (c: Content) => ({ ...c, ticket: { ...c.ticket!, title: 'Something else' } })],
  ])('opens for %s', (_n, change) => {
    expect(alsoSigned(change(hostContent()), typed).differs).toBe(true)
  })
})

describe('the quick review', () => {
  it('shows the typed values and the sentence always; the fold is closed, named with a count and one click away', () => {
    render(<SignContent content={hostContent()} quick={typed} />)
    const visible = screen.getByTestId('sign-typed')
    expect(within(visible).getByText('Rename the team photo folder')).toBeVisible()
    expect(within(visible).getByText('Use the 2026 naming.')).toBeVisible()
    expect(visible).toHaveTextContent('Links still work (checked by npm run links)')
    expect(screen.getByTestId('sign-also-sentence')).toHaveTextContent('This also approves its requirements and plan, made from these checks.')
    const fold = screen.getByTestId('sign-also')
    expect(fold).not.toHaveAttribute('open')
    expect(fold.querySelector('summary')).toHaveTextContent('Also signed (7)')
    // Every derived row is in the fold (rendered, only collapsed).
    expect(fold).toHaveTextContent('Acceptance checks')
    expect(fold).toHaveTextContent('Chore')
  })
  it('opens by itself, saying so, when a derived row differs', () => {
    const c = hostContent()
    render(<SignContent content={{ ...c, sections: c.sections!.map((s) => (s.id === 'plan' ? { ...s, text: 'Do something else' } : s)) }} quick={typed} />)
    const fold = screen.getByTestId('sign-also')
    expect(fold).toHaveAttribute('open')
    expect(fold.querySelector('summary')).toHaveTextContent('differs from what you typed')
    expect(screen.getByTestId('sign-also-sentence')).toBeVisible()
  })
})
