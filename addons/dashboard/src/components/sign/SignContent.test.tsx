import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it } from 'vitest'
import { SignContent } from './SignContent'

afterEach(cleanup)

it('shows the signed identity tail, host key qualification, task id and before qualification', () => {
  render(<SignContent content={{
    headline: 'Review',
    ticket: { key: 'TST-0001', title: '', uid_tail: 'ab12cd34', key_note: 'key per host' },
    tasks: [{ n: 1, id: 'T-3', title: 'Check it ⏎ 2. forged task', verify: ['npm test ⏎ echo done'] }],
    fields_changed: [{ field: 'title', before: 'Old', after: 'New', before_note: 'per host, not signed' }],
    notes: ['Host note'], note: ['Legacy list note'],
  }} />)
  expect(screen.getByText('TST-0001').parentElement).toHaveTextContent('TST-0001 · …ab12cd34')
  // The host's key qualification is technical: folded away, not in the plain view.
  expect(screen.getByText('key per host').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByRole('listitem')).toHaveTextContent('T-3 · Check it ⏎ 2. forged task')
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
  expect(screen.getByText('Old').parentElement).toHaveTextContent('Old (per host, not signed) → New')
  expect(screen.getByText('Host note')).toBeVisible()
  expect(screen.getByText('Legacy list note')).toBeVisible()
})

it('marks and indents every block and continuation without creating items or rows', () => {
  render(<SignContent content={{
    headline: 'Review',
    sections: [{ id: 'plan', title: 'Plan', text: 'Section\nForged section' }],
    acceptance: [{ id: 'AC1', text: 'Criterion\nAC2 forged' }],
    tasks: [{ n: 1, id: 'T-3', title: 'Task\n2. forged', verify: ['Check\nVerify: forged'] }],
    fields_changed: [{ field: 'title', before: 'Before\nfield: forged before', after: 'After\nfield: forged after' }],
    note: 'Single note',
  }} />)
  for (const text of ['Section', 'Forged section', 'Criterion', 'AC2 forged', 'Task', '2. forged', 'Check', 'Verify: forged', 'Before', 'field: forged before', 'After', 'field: forged after']) {
    const line = screen.getByText(text).parentElement!
    expect(line).toHaveClass('pl-4', 'flex')
    expect(line.firstElementChild).toHaveTextContent('|')
  }
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByText('Single note')).toBeVisible()
})

it('renders every display string in fully populated content', () => {
  const content = {
    headline: 'Review all changes',
    ticket: { key: 'FULL-0001', title: 'Complete ticket title', uid_tail: '1234abcd', key_note: 'Host-local ticket key' },
    sections: [{ id: 'plan', title: 'Complete plan', text: 'First section line\nSecond section line' }],
    acceptance: [{ id: 'AC-yes', text: 'Checked criterion', checked: true }, { id: 'AC-no', text: 'Unchecked criterion', checked: false }],
    tasks: [{ n: 3, id: 'TASK-three', title: 'Complete task', verify: ['First verification', 'Second verification'] }],
    fields_changed: [
      { field: 'title', before: 'Previous title', before_note: 'Previous value from host', after: 'Updated title' },
      { field: 'removed', before: 'Removed value', after: null },
      { field: 'added', before: null, after: 'Added value' },
    ],
    notes: ['Current note one', 'Current note two'],
    note: ['Legacy note one', 'Legacy note two'],
  }
  // The dialog owns the headline; section ids are structural keys, not display values.
  render(<><h2>{content.headline}</h2><SignContent content={content} /></>)
  for (const text of [
    content.headline, content.ticket.key, content.ticket.title, content.ticket.uid_tail,
    content.sections[0].title, ...content.sections[0].text.split('\n'),
    ...content.acceptance.flatMap(a => [a.id, a.text]),
    ...content.tasks.flatMap(t => [t.id, t.title, ...t.verify]),
    ...content.fields_changed.flatMap(f => [f.field, f.before, f.before_note, f.after]).filter((v): v is string => typeof v === 'string'),
    ...content.notes, ...content.note,
  ]) expect(screen.getByText(text)).toBeVisible()
  expect(screen.getByText(content.ticket.key_note)).toBeInTheDocument() // folded under the technical details
  expect(screen.getByLabelText('Checked')).toBeVisible()
  expect(screen.getByLabelText('Unchecked')).toBeVisible()
  expect(screen.getByText('TASK-three').closest('li')).toHaveAttribute('value', '3')
  expect(screen.getAllByText('—')).toHaveLength(2)
})

const SEV = 'p_181d953f12ab34cd56ef78901234abcd'
const MARA = 'p_aaaa11112222333344445555666677'
function withMembers(members: { person: string; name: string }[], fields: { field: string; before: string | null; after: string | null }[], key_note = 'key per host, not proven') {
  const client = new QueryClient()
  client.setQueryData(['workspaces'], [{ id: 'w_1', members }])
  return render(<QueryClientProvider client={client}><SignContent content={{ headline: 'New ticket', ticket: { key: 'RA-0001', title: '', uid_tail: 'ab12cd34', key_note }, fields_changed: fields }} /></QueryClientProvider>)
}
const row = (field: string) => screen.getByText(field).parentElement!

it('shows a known member as name WITH short id, the full id and the key line only under Technical details', () => {
  withMembers([{ person: SEV, name: 'Severin' }, { person: MARA, name: 'Mara' }], [{ field: 'owner', before: null, after: SEV }, { field: 'reviewer', before: MARA, after: 'p_0badc0de99887766' }])
  expect(row('owner')).toHaveTextContent('owner: — → Severin (p_181d953f12ab…)')
  expect(row('owner')).not.toHaveTextContent(SEV)
  expect(row('reviewer')).toHaveTextContent('reviewer: Mara (p_aaaa11112222…) → p_0badc0de9988… (unknown person)')
  const details = screen.getByText(/Technical details/).closest('details')!
  expect(details).toHaveTextContent(`Severin (p_181d953f12ab…) = ${SEV}`)
  expect(details).toHaveTextContent('key per host, not proven')
})

it('a cut id never resolves by prefix: it shows as unknown person', () => {
  withMembers([{ person: SEV, name: 'Severin' }], [{ field: 'owner', before: null, after: 'p_181d953f…' }])
  expect(row('owner')).toHaveTextContent('owner: — → p_181d953f… (unknown person)')
  expect(row('owner')).not.toHaveTextContent('Severin')
})

it('two members with the same display name are told apart by id, with no name shown', () => {
  withMembers([{ person: 'p_aaaa1111222233334444', name: 'Severin' }, { person: 'p_bbbb1111222233334444', name: 'Severin' }], [{ field: 'owner', before: 'p_aaaa1111222233334444', after: 'p_bbbb1111222233334444' }])
  expect(row('owner')).toHaveTextContent('owner: p_aaaa11112222… (name shared by 2 members) → p_bbbb11112222… (name shared by 2 members)')
  expect(row('owner')).not.toHaveTextContent('Severin')
})

it('an id that is not a member of any known workspace says unknown person', () => {
  withMembers([{ person: SEV, name: 'Severin' }], [{ field: 'owner', before: null, after: 'p_deadbeef01aabbccdd' }])
  expect(row('owner')).toHaveTextContent('p_deadbeef01aa… (unknown person)')
})

it('confusable, case-different and zero-width duplicate names still count as shared, and render without controls', () => {
  withMembers([{ person: 'p_aaaa1111222233334444', name: 'Severin' }, { person: 'p_bbbb1111222233334444', name: 'S\u200Be\u202Ev\uFF45rin' }, { person: 'p_cccc1111222233334444', name: 'SEVERIN' }], [{ field: 'owner', before: 'p_aaaa1111222233334444', after: 'p_cccc1111222233334444' }])
  expect(row('owner')).toHaveTextContent('p_aaaa11112222… (name shared by 3 members) → p_cccc11112222… (name shared by 3 members)')
})

it('strips zero-width and bidi controls from a name it does show', () => {
  withMembers([{ person: SEV, name: 'Sev\u202Eerin\u200B' }], [{ field: 'owner', before: null, after: SEV }])
  expect(row('owner').textContent).toContain('Severin (p_181d953f12ab…)')
  expect(row('owner').textContent).not.toMatch(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069]/)
})

it('resolves a real-host shaped fixture: full 32-hex person id from core, not a prefix', () => {
  const full = 'p_' + '181d953f12ab34cd56ef789012345678'
  withMembers([{ person: full, name: 'Severin' }], [{ field: 'owner', before: null, after: full }])
  expect(row('owner')).toHaveTextContent('owner: — → Severin (p_181d953f12ab…)')
})
