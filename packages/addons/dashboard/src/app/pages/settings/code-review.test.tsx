import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }

// Owner ruling 2026-10-10: the signed covers name exactly the tickets a code review policy moves.
describe('Settings → Gates → Code review', () => {
  it('turning it on names the done tickets with an open landing that move back to testing', async () => {
    const { user } = renderApp('/settings/gates')
    await user.click(await screen.findByRole('radio', { name: 'Every ticket' }, T))
    const dialog = await screen.findByRole('dialog', {}, T)
    const covers = within(dialog).getByText('Covers').nextElementSibling!
    expect(covers).toHaveTextContent('Moves back to testing: DEMO-0052 (1)')
    expect(covers).toHaveTextContent('Only done tickets with an open landing (queued, checking or failed) go back; landed ones stay done')
  })
  it('a change that moves nothing says so', async () => {
    const { user } = renderApp('/settings/gates')
    await user.click(await screen.findByRole('button', { name: 'Code review: 2 approvals' }, T))
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).getByText('Covers').nextElementSibling).toHaveTextContent('Moves nothing')
  })
})
