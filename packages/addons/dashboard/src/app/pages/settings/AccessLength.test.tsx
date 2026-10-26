import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { AccessLength } from './AccessLength'

it('offers all four limits with keyboard access and the current selection', async () => {
  const change = vi.fn()
  const user = userEvent.setup()
  render(<AccessLength value={8} disabled={false} onChange={change} />)
  expect(screen.getByRole('group', { name: 'Agents may work for you for up to' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '8 h' })).toHaveAttribute('aria-pressed', 'true')
  for (const [label, hours] of [['8 h', 8], ['1 day', 24], ['3 days', 72], ['7 days', 168]] as const) {
    screen.getByRole('button', { name: label }).focus()
    await user.keyboard('{Enter}')
    expect(change).toHaveBeenLastCalledWith(hours)
  }
})
it('is read-only for others and displays custom existing limits honestly', () => {
  render(<AccessLength value={12} disabled onChange={vi.fn()} />)
  expect(screen.getByText('Current limit: 12 h')).toBeInTheDocument()
  for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled()
})
