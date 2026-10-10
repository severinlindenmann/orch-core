import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 6000 }

// Owner feedback D: Today's addon cards are one calm "Glance" list, in the language of the core rows.
describe('Today: Glance', () => {
  it('is one list with neutral borders: no addon frame, no card in a card, one A per item', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const glance = await screen.findByRole('region', { name: 'Glance' }, T)
    const items = await within(glance).findAllByRole('listitem', {}, T)
    const headed = items.filter((li) => li.querySelector(':scope > div > h3'))
    expect(headed.map((li) => li.querySelector('h3')!.textContent)).toEqual(['Apps & shares', 'PRs needing review · 3', 'Agent spend'])
    for (const li of headed) expect(within(li).getAllByRole('img', { name: /^From the .* addon$/ })).toHaveLength(1)
    // No orange frame and no boxed stat inside the list.
    expect(glance.querySelector('.border-addon-border')).toBeNull()
    expect(glance.querySelector('.rounded-md.border')).toBeNull()
  })
  it('each item reads as title, one key number, one secondary line and "Open" to the addon page', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const glance = await screen.findByRole('region', { name: 'Glance' }, T)
    expect(await within(glance).findByText('1 of 3', {}, T)).toBeInTheDocument()
    expect(within(glance).getByText('1 failed build · 5 live shares · 236 views')).toBeInTheDocument()
    expect(within(glance).getByText('CHF 31.40')).toBeInTheDocument()
    expect(within(glance).getByText('CHF 39.91 this month')).toBeInTheDocument()
    // Agent spend: one line with a sparkline of the last 7 days.
    expect(within(glance).getByRole('img', { name: /^Trend over 7 values/ })).toBeInTheDocument()
    expect(within(glance).getByRole('link', { name: 'Open Apps & shares' })).toHaveAttribute('href', '/addon/publish/shares')
    expect(within(glance).getByRole('link', { name: 'Open PRs needing review' })).toHaveAttribute('href', '/addon/github/reviews')
    expect(within(glance).getByRole('link', { name: 'Open Agent spend' })).toHaveAttribute('href', '/addon/usage/overview')
  })
})
