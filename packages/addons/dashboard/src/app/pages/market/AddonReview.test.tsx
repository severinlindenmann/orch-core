import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import type { Workspace } from '@/api/types'
import { addonContent, capabilityWords } from './AddonReview'
import type { SignContent } from '@/api/signing'
import { BuiltInAddOns } from '../settings/host/AddOns'
// Allow the route and signing review to settle on busy development machines.
configure({ asyncUtilTimeout: 15000 })
vi.mock('../settings/host/useViewerRole', () => ({ useViewerRole: () => 'owner' }))

vi.mock('@/api/click', async importOriginal => ({ ...await importOriginal<object>(), clickReady: () => true }))
vi.mock('@/api/hubsign', async importOriginal => ({ ...await importOriginal<object>(), hubOnly: () => false }))
const content = (access: boolean): SignContent => ({ headline: 'Turn on Estimate', summary: { title: 'Turn on Estimate', lines: [access ? 'It says it will use the internet.' : 'It declares no capabilities.', 'Version 0.1.0. Runs on this computer.'], risk: 'An add-on runs its own code on this computer as you; turn it on only if you trust it.' }, fields_changed: [{ field: 'version', before: null, after: '0.1.0' }] })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('add-on review words', () => {
  it.each([[false, false], [false, true], [true, false], [true, true]])('keeps what the host sends (marketplace=%s, access=%s)', (marketplace, access) => {
    const original = content(access)
    const result = addonContent(original, marketplace)
    expect(result.summary?.risk).toBe(original.summary?.risk) // the trust line is the host's call, never filtered here
    expect(result.summary?.lines.join(' ')).not.toContain('Version')
    expect(result.fields_changed).toEqual(original.fields_changed)
    if (!access) expect(result.summary?.lines).toEqual(['It declares no capabilities.'])
  })
  it('uses plain capability names and preserves unfamiliar names', () => {
    expect(['files', 'pty', 'network', 'spawn_agent', 'future_permission'].map(capabilityWords)).toEqual(['files', 'terminals', 'internet', 'agents', 'future_permission'])
  })
})

beforeEach(() => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: 'req', expires_at: new Date(Date.now() + 600000).toISOString(), op: 'addon.grant', confirm: 'click' })
  vi.spyOn(api, 'viewClick').mockResolvedValue({ view_digest: 'sha256:' + 'a'.repeat(64), view: { risk: CLICK_RISK, op: 'addon.grant', workspace: { id: 'ws', name: 'Test', prefix: 'T' }, fingerprint: '0123456789ab', lines: ['version 0.1.0'], checks: [], confirm: { kind: 'click' }, expires_at: new Date(Date.now() + 600000).toISOString(), auth: 'convenience', device: 'd_test', artifacts: null, content: content(false) } })
})
it('a synthetic add-on switch press cannot prepare or sign', async () => {
  const sign = vi.spyOn(api, 'signClick').mockImplementation(() => new Promise(() => {}))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BuiltInAddOns workspace={{ id: 'ws' } as Workspace} rows={[{ id: 'estimate', title: 'Estimate', desc: '', state: 'off', locked: null, blocked: null, version: '0.1.0' }]} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('switch', { name: 'Estimate' }))
  expect(api.prepareClick).not.toHaveBeenCalled()
  expect(sign).not.toHaveBeenCalled()
  expect(screen.getByTestId('addon-estimate-accordion')).toBeInTheDocument()
})
