// Today lays out by the page width (the window minus a right-hand dock), not the window width (M1 review, owner's 13").
import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
afterEach(() => vi.unstubAllGlobals())

describe('Today columns follow the page width', () => {
  it('a 1440 px window without the dock: two columns (the Agents panel beside the queue)', async () => {
    vi.stubGlobal('innerWidth', 1440)
    renderApp('/')
    await screen.findByRole('region', { name: 'Needs you' }, T)
    expect(screen.getByRole('link', { name: /All agents/ })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Agents' })).toBeNull()
  })
  it('the same window with the dock open on the right: one column (the Agents bar above the queue)', async () => {
    vi.stubGlobal('innerWidth', 1440)
    renderApp('/', { storage: { 'orch.dock.p_sev': JSON.stringify({ side: 'right', open: true, bottom: 280, right: 720, harness: 'claude' }) } })
    await screen.findByRole('region', { name: 'Needs you' }, T)
    expect(within(screen.getByRole('region', { name: 'Agents' })).getByRole('button', { name: 'Show' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /All agents/ })).toBeNull()
  })
})

it.each([
  ['closed', 'bottom', false, 280, true],
  ['bottom', 'bottom', true, 280, true],
  ['right-open', 'right', true, 400, false],
  ['right-collapsed', 'right', false, 400, true],
  ['max', 'right', true, 9999, false],
] as const)('1280 px with %s dock', async (_name, side, open, right, wide) => {
  vi.stubGlobal('innerWidth', 1280)
  renderApp('/', { storage: { 'orch.dock.p_sev': JSON.stringify({ side, open, right, bottom: 280, harness: 'claude' }) } })
  await screen.findByRole('region', { name: 'Needs you' }, T)
  expect(!!screen.queryByRole('link', { name: /All agents/ })).toBe(wide)
})
