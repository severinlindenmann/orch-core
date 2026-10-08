import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const rail = () => screen.getByRole('complementary', { name: 'Ticket details' })
const openRail = () => screen.findByRole('complementary', { name: 'Ticket details' })
const sumNow = async () => {
  const sum = await within(await screen.findByRole('region', { name: 'In progress' })).findByLabelText(/^Sum of /)
  return Number(/(\d+) pts?$/.exec(sum.textContent ?? '')?.[1])
}

describe('estimate on the board and the ticket', () => {
  it('shows a points sum with the A badge in each column that has estimates', async () => {
    renderApp('/board')
    const ws = mockStore.workspaces[0].id
    const tickets = await api.listTickets(ws)
    const want = tickets.filter((t) => t.status === 'in-progress').reduce((n, t) => n + (Number((t.addons?.estimate as { points?: number } | undefined)?.points) || 0), 0)
    expect(want).toBeGreaterThan(0)
    const col = await screen.findByRole('region', { name: 'In progress' })
    const sum = await within(col).findByLabelText(/^Sum of /)
    expect(sum).toHaveTextContent(`${want} pts`)
    expect(within(sum).getByRole('img', { name: 'From addon: estimate' })).toBeInTheDocument()
  })

  it('the column sum rises by exactly the new points after estimating a card', async () => {
    const { user } = renderApp('/board')
    const w0 = Number(mockStore.ticket('DEMO-0043')!.addons.estimate?.points ?? 0)
    const before = await sumNow()
    await user.click(await screen.findByTestId('card-DEMO-0043'))
    await user.selectOptions(await within(await openRail()).findByLabelText('Points'), '21')
    await user.click(within(rail()).getByRole('button', { name: 'Save estimate' }))
    await screen.findByText(/DEMO-0043 estimated at 21 points\./)
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    await waitFor(async () => expect(await sumNow()).toBe(before - w0 + 21))
  })

  it('a t-shirt size shows its label on the card and adds its fixed weight (L = 5) to the sum', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 't-shirt')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText(/Settings saved/)
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    const w0 = Number(mockStore.ticket('DEMO-0043')!.addons.estimate?.points ?? 0)
    const before = await sumNow()
    await user.click(await screen.findByTestId('card-DEMO-0043'))
    const select = await within(await openRail()).findByLabelText('Points')
    await waitFor(() => expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(expect.arrayContaining(['S', 'M', 'L'])))
    await user.selectOptions(select, 'L')
    await user.click(within(rail()).getByRole('button', { name: 'Save estimate' }))
    await screen.findByText('DEMO-0043 estimated at L.')
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    expect(await within(await screen.findByTestId('card-DEMO-0043')).findByText('L')).toBeInTheDocument()
    await waitFor(async () => expect(await sumNow()).toBe(before - w0 + 5))
  })

  it('writes "pts" in the column header', async () => {
    renderApp('/board')
    const sum = await within(await screen.findByRole('region', { name: 'In progress' })).findByLabelText(/^Sum of /)
    expect(sum.textContent).toMatch(/\d+ pts$/)
  })
})
