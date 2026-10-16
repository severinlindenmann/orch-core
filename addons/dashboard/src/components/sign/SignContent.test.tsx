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
