import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }

describe('Artifacts page', () => {
  it('is a core nav entry after Tickets', async () => {
    renderApp('/')
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    const names = within(nav).getAllByRole('link').map((l) => l.getAttribute('aria-label') ?? l.textContent)
    expect(names.indexOf('Artifacts')).toBe(names.indexOf('Tickets') + 1)
  })
  it('lists the workspace artifacts with ticket, adder and time; filters show their counts', async () => {
    const { user } = renderApp('/artifacts')
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
    const row = screen.getByRole('button', { name: 'Open tariff-export.log' }).closest('tr')!
    expect(within(row).getByText('DEMO-0043')).toBeInTheDocument()
    expect(within(row).getByText('Claude Code for Severin')).toBeInTheDocument()
    for (const f of ['Kind', 'Ticket', 'Added by', 'Added']) expect(screen.getByRole('combobox', { name: f })).toBeInTheDocument()
    // Narrowing and clearing (the menus are Radix selects, exercised in the browser pass; the host filters are in mocks/artifacts.test.ts).
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), '.log')
    expect(await screen.findByText('4 artifacts', {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
  })
  it('searches by name', async () => {
    const { user } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.type(screen.getByRole('searchbox', { name: 'Search artifacts' }), 'tolerance')
    expect(await screen.findByText('1 artifact', {}, T)).toBeInTheDocument()
  })
  it('opens an artifact in the drawer and returns focus to it on close', async () => {
    const { user } = renderApp('/artifacts')
    const open = await screen.findByRole('button', { name: 'Open tariff-export.log' }, T)
    await user.click(open)
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(within(sheet).getByText(/On/)).toHaveTextContent('DEMO-0043')
    expect(await within(sheet).findByRole('button', { name: /Copy all/ }, T)).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(open).toHaveFocus(), T)
  })
  it('runs an HTML report in the sandboxed frame only while agent HTML is on', async () => {
    const { user } = renderApp('/artifacts')
    await user.click(await screen.findByRole('button', { name: 'Open reconciliation-demo.html' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(await within(sheet).findByTitle(/Sandboxed preview of reconciliation-demo.html/, {}, T)).toHaveAttribute('sandbox')
  })
  it('switches to the grid and remembers it per person', async () => {
    const { user, unmount } = renderApp('/artifacts')
    await screen.findByText('12 artifacts', {}, T)
    await user.click(screen.getByRole('radio', { name: 'Grid' }))
    expect(await screen.findByRole('list', { name: 'Artifacts' })).toBeInTheDocument()
    expect(localStorage.getItem('orch.artifacts.view.p_sev')).toBe('grid')
    unmount()
  })
  it('Tom does not see the artifacts of a restricted ticket (the host leaves them out)', async () => {
    const setup = (s: MockStore) => {
      s.append('DEMO-0044', { type: 'artifact.added', actor: 'p_sev', name: 'incident-notes.md', kind: 'report', bytes: 900, sha256: 'a'.repeat(64) })
    }
    renderApp('/artifacts', { viewer: 'p_tom', setup })
    expect(await screen.findByText('12 artifacts', {}, T)).toBeInTheDocument()
    expect(screen.queryByText('incident-notes.md')).not.toBeInTheDocument()
  })
})
