import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const rail = () => screen.getByRole('complementary', { name: 'Ticket details' })
const sumOf = (column: string) => within(screen.getByRole('region', { name: column })).queryByLabelText(/^Sum of /)

describe('estimate on the board and the ticket', () => {
  it('shows a points sum with the A badge in each column that has estimates', async () => {
    renderApp('/board')
    const ws = mockStore.workspaces[0].id
    const tickets = await api.listTickets(ws)
    const want = tickets.filter((t) => t.status === 'in-progress').reduce((n, t) => n + (Number((t.addons?.estimate as { points?: number } | undefined)?.points) || 0), 0)
    expect(want).toBeGreaterThan(0)
    const col = await screen.findByRole('region', { name: 'In progress' })
    const sum = await within(col).findByLabelText(/^Sum of /)
    expect(sum).toHaveTextContent(`${want} pt`)
    expect(within(sum).getByRole('img', { name: 'From addon: estimate' })).toBeInTheDocument()
  })

  it('the column sum updates after estimating a card', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1 })
    const select = await within(rail()).findByLabelText('Points')
    await user.selectOptions(select, '21')
    await user.click(within(rail()).getByRole('button', { name: 'Save estimate' }))
    await screen.findByText(/estimated at 21 points/)
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    const col = await screen.findByRole('region', { name: 'In progress' })
    const sum = await within(col).findByLabelText(/^Sum of /)
    const m = /(\d+) pt/.exec(sum.textContent ?? '')
    expect(Number(m?.[1])).toBeGreaterThanOrEqual(21)
    const tickets = await api.listTickets(mockStore.workspaces[0].id)
    const total = tickets.filter((t) => t.status === 'in-progress').reduce((n, t) => n + (Number((t.addons?.estimate as { weight?: number; points?: number } | undefined)?.weight ?? (t.addons?.estimate as { points?: number } | undefined)?.points) || 0), 0)
    expect(Number(m?.[1])).toBe(total)
  })

  it('switching the scale to t-shirt shows sizes in the form and the card shows the label', async () => {
    const { user } = renderApp('/settings/addon/estimate')
    await user.selectOptions(await screen.findByLabelText(/Scale/), 't-shirt')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText(/Settings saved/)
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    await screen.findByRole('region', { name: 'In progress' })
    await user.click(await screen.findByTestId('card-DEMO-0043'))
    const select = await within(rail()).findByLabelText('Points')
    await waitFor(() => expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(expect.arrayContaining(['S', 'M', 'L'])))
    await user.selectOptions(select, 'L')
    await user.click(within(rail()).getByRole('button', { name: 'Save estimate' }))
    await screen.findByText(/estimated at L/)
    await user.click(screen.getAllByRole('link', { name: 'Board' })[0])
    expect(await within(await screen.findByTestId('card-DEMO-0043')).findByText('L')).toBeInTheDocument()
    expect(sumOf('In progress')).toHaveTextContent(/\d+ pt/)
  })
})
