import { cleanup, render, screen } from '@testing-library/react'
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
  expect(screen.getByText('key per host')).toBeVisible()
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
    content.headline, ...Object.values(content.ticket),
    content.sections[0].title, ...content.sections[0].text.split('\n'),
    ...content.acceptance.flatMap(a => [a.id, a.text]),
    ...content.tasks.flatMap(t => [t.id, t.title, ...t.verify]),
    ...content.fields_changed.flatMap(f => [f.field, f.before, f.before_note, f.after]).filter((v): v is string => typeof v === 'string'),
    ...content.notes, ...content.note,
  ]) expect(screen.getByText(text)).toBeVisible()
  expect(screen.getByLabelText('Checked')).toBeVisible()
  expect(screen.getByLabelText('Unchecked')).toBeVisible()
  expect(screen.getByText('TASK-three').closest('li')).toHaveAttribute('value', '3')
  expect(screen.getAllByText('—')).toHaveLength(2)
})
