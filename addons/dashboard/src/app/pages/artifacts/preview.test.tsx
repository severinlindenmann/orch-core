import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'

// jsdom lays nothing out: the page "measures" 1200 px here, as on a 1440 px window with the sidebar open.
vi.mock('@/lib/useElementWidth', () => ({ useElementWidth: () => [() => {}, 1200] }))

const T = { timeout: 4000 }
const original = window.matchMedia
const wide = (on: boolean) => {
  window.matchMedia = ((query: string) => ({
    matches: (on && query === '(min-width: 1280px)') || query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  window.matchMedia = original
})

const selectedRow = () => document.querySelector('tr[aria-current="true"]')

// Owner feedback E: in the list view a selected row previews beside the table; j/k move the selection.
describe('Artifacts list preview', () => {
  it('a wide window: selecting a row opens the preview pane beside the table, not the drawer', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Open tariff-export.log' }, T))
    const pane = await screen.findByRole('complementary', { name: 'Preview of tariff-export.log' }, T)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(within(pane).getByText(/On/)).toHaveTextContent('DEMO-0043')
    expect(await within(pane).findByRole('button', { name: /Copy all/ }, T)).toBeInTheDocument()
    expect(selectedRow()).toHaveTextContent('tariff-export.log')
    // Beside the pane the table keeps Name, Kind, Ticket and Added.
    expect(screen.queryByRole('columnheader', { name: 'Size' })).toBeNull()
  })
  it('j and k move the selection and the preview follows; typing in the search box does not', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Open tariff-export.log' }, T))
    await screen.findByRole('complementary', { name: 'Preview of tariff-export.log' }, T)
    const rows = [...document.querySelectorAll('tbody tr')]
    const at = rows.findIndex((r) => r === selectedRow())
    const next = rows[at + 1].querySelector('td')!.textContent!
    await user.keyboard('j')
    await waitFor(() => expect(selectedRow()).toBe(rows[at + 1]))
    expect(await screen.findByRole('complementary', { name: new RegExp(`^Preview of ${next.split(/\s/)[0].replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }, T)).toBeInTheDocument()
    await user.keyboard('k')
    await waitFor(() => expect(selectedRow()).toBe(rows[at]))
    await screen.findByRole('complementary', { name: 'Preview of tariff-export.log' }, T)
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'j')
    expect(selectedRow()).toBe(rows[at])
  })
  it('an HTML report in the pane runs in the sandboxed frame, as in the drawer', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Open reconciliation-demo.html' }, T))
    const pane = await screen.findByRole('complementary', { name: 'Preview of reconciliation-demo.html' }, T)
    expect(await within(pane).findByTitle(/Sandboxed preview of reconciliation-demo.html/, {}, T)).toHaveAttribute('sandbox')
  })
  it('closing the pane puts focus back on the row', async () => {
    wide(true)
    const { user } = renderApp('/artifacts')
    const open = await screen.findByRole('button', { name: 'Open tariff-export.log' }, T)
    await user.click(open)
    await user.click(await screen.findByRole('button', { name: 'Close the preview' }, T))
    expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull()
    await waitFor(() => expect(open).toHaveFocus())
  })
  it('a narrower window keeps the drawer', async () => {
    wide(false)
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Open tariff-export.log' }, T))
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: /Preview of/ })).toBeNull()
  })
})
