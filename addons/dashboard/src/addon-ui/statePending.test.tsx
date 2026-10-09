import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { renderApp } from '@/test/renderApp'
import { resolveBindings } from './bindings'

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
    renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
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
