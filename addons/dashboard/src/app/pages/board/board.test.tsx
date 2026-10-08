import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('board page', () => {
  it('renders status columns with DEMO-0043 under in-progress', async () => {
    renderApp('/board')
    const col = await screen.findByRole('region', { name: 'In progress' })
    expect(await within(col).findByTestId('card-DEMO-0043')).toBeInTheDocument()
    for (const name of ['Backlog', 'Open', 'Waiting', 'Testing', 'Done']) expect(screen.getByRole('region', { name })).toBeInTheDocument()
  })

  it('filter Mine reduces the cards', async () => {
    const { user } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043')
    const before = screen.getAllByTestId(/^card-/).length
    await user.click(screen.getByRole('button', { name: 'Mine' }))
    const after = screen.getAllByTestId(/^card-/).length
    expect(after).toBeGreaterThan(0)
    expect(after).toBeLessThan(before)
  })

  it('shows the github lane with the addon badge and import buttons', async () => {
    renderApp('/board')
    const lane = await screen.findByRole('region', { name: /GitHub issues/ })
    expect(within(lane).getByRole('img', { name: 'From addon: github' })).toBeInTheDocument()
    expect(within(lane).getAllByRole('button', { name: /Import as ticket/ })).toHaveLength(3)
  })
})
