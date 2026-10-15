import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'
import { resolveBindings } from './bindings'

// Ticket-rail tests render at 1440 px (the rail is a column from 1280 px; below, the Panels sheet).
afterEach(() => vi.unstubAllGlobals())

const T = { timeout: 8000 }

/** Holds the start-agent state request until `release` (other addons answer at once). */
function holdState(addon: string) {
  const real = api.getAddonState
  let release: () => void = () => {}
  const gate = new Promise<void>((r) => (release = r))
  vi.spyOn(api, 'getAddonState').mockImplementation((ws, name, ...rest) => (name === addon ? gate.then(() => real(ws, name, ...rest)) : real(ws, name, ...rest)))
  return () => release()
}

afterEach(() => vi.restoreAllMocks())

describe('addon surfaces wait for their state', () => {
  it('an addon page shows a skeleton until its state has loaded, never the could-not-be-drawn box', async () => {
    const release = holdState('start-agent')
    renderApp('/addon/start-agent/start', { viewer: 'p_sev' })
    expect(await screen.findByRole('status', { name: 'Loading Start agent' }, T)).toBeInTheDocument()
    expect(screen.queryByText(/could not be drawn/)).toBeNull()
    expect(screen.queryByText(/could not be shown/)).toBeNull()
    release()
    expect(await screen.findByRole('combobox', { name: /Ticket/ }, T)).toBeInTheDocument()
    expect(screen.queryByText(/could not be drawn/)).toBeNull()
  })

  it('a failed state load shows a calm error with Retry, and Retry loads the page', async () => {
    const real = api.getAddonState
    const spy = vi.spyOn(api, 'getAddonState').mockImplementation((ws, name, ...rest) =>
      name === 'start-agent' ? Promise.reject(new ApiError(500, { code: 'internal', message: 'boom', retryable: true })) : real(ws, name, ...rest),
    )
    const { user } = renderApp('/addon/start-agent/start', { viewer: 'p_sev' })
    const alert = await screen.findByRole('alert', {}, T)
    expect(alert).toHaveTextContent(/could not be loaded/)
    expect(screen.queryByText(/could not be drawn/)).toBeNull()
    spy.mockRestore()
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('combobox', { name: /Ticket/ }, T)).toBeInTheDocument()
  })

  it('a ticket panel that binds addon state waits for it too', async () => {
    const release = holdState('start-agent')
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await openTicketPanel(user, 'Start agent')
    expect(await screen.findByRole('status', { name: 'Loading Start agent' }, T)).toBeInTheDocument()
    expect(screen.queryByText(/could not be (drawn|shown)/)).toBeNull()
    release()
    await waitFor(() => expect(screen.queryByRole('status', { name: 'Loading Start agent' })).toBeNull(), T)
    expect(await screen.findByRole('button', { name: 'Start' }, T)).toBeInTheDocument()
  })
})

describe('resolveBindings defaults', () => {
  it('a oneOf or enum bound to nothing becomes an empty list, not null', () => {
    const node = { schema: { properties: { t: { type: 'string', oneOf: { $ref: 'addon.ticketOptions' }, enum: { $ref: 'addon.nope' } } } } }
    expect(resolveBindings(node, {})).toEqual({ schema: { properties: { t: { type: 'string', oneOf: [], enum: [] } } } })
  })
  it('other null bindings stay null', () => {
    expect(resolveBindings({ value: { $ref: 'addon.nope' } }, {})).toEqual({ value: null })
  })
})

describe('state requests per surface', () => {
  it('the busy board asks each addon for its state at most once, and never per card', async () => {
    const spy = vi.spyOn(api, 'getAddonState')
    // Flat: every card is on screen (grouped, the big epics start folded).
    renderApp('/board', { viewer: 'p_sev', setup: (s) => s.reset('busy'), storage: { 'orch.board.display.p_sev': JSON.stringify({ group: 'none' }) } })
    await waitFor(() => expect(screen.getAllByTestId(/^card-DEMO-/).length).toBeGreaterThan(50), { timeout: 15_000 })
    await new Promise((r) => setTimeout(r, 300))
    expect(spy.mock.calls.filter((c) => c[2] !== undefined)).toEqual([])
    const perAddon = new Map<string, number>()
    for (const c of spy.mock.calls) perAddon.set(c[1], (perAddon.get(c[1]) ?? 0) + 1)
    for (const [name, n] of perAddon) expect(n, name).toBeLessThanOrEqual(1)
  }, 30_000)
  it('the grouped busy board (every lane unfolded) asks each addon for its state at most once, and never per ticket', async () => {
    const spy = vi.spyOn(api, 'getAddonState')
    const lanes = Object.fromEntries(['DEMO-0040', 'DEMO-0050', 'DEMO-0100', 'DEMO-0101', 'DEMO-0102', '_none'].map((k) => [k, false]))
    renderApp('/board', { viewer: 'p_sev', setup: (s) => s.reset('busy'), storage: { 'orch.board.display.p_sev': JSON.stringify({ group: 'epic', lanes }) } })
    await waitFor(() => expect(screen.getAllByTestId(/^card-DEMO-/).length).toBeGreaterThan(50), { timeout: 15_000 })
    expect(document.querySelector('[data-grouped="epic"]')).not.toBeNull()
    await new Promise((r) => setTimeout(r, 300))
    expect(spy.mock.calls.filter((c) => c[2] !== undefined)).toEqual([])
    const perAddon = new Map<string, number>()
    for (const c of spy.mock.calls) perAddon.set(c[1], (perAddon.get(c[1]) ?? 0) + 1)
    for (const [name, n] of perAddon) expect(n, name).toBeLessThanOrEqual(1)
  }, 30_000)
  it('a ticket panel that binds addon state asks for its ticket', async () => {
    const spy = vi.spyOn(api, 'getAddonState')
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await openTicketPanel(user, 'Start agent')
    expect(await screen.findByRole('button', { name: 'Start' }, T)).toBeInTheDocument()
    expect(spy.mock.calls.some((c) => c[1] === 'start-agent' && c[2] === 'DEMO-0044')).toBe(true)
  })
})
