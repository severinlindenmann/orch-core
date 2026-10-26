import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configure, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

vi.mock('@/components/sign/SignMethods', () => ({ useSignMethods: () => ({ allowed: ['click'], loading: false }) }))
vi.mock('@/api/click', async importOriginal => ({ ...await importOriginal<object>(), clickReady: () => true }))
vi.mock('@/api/hubsign', async importOriginal => ({ ...await importOriginal<object>(), hubOnly: () => false }))
const content = (access: boolean): SignContent => ({ headline: 'Turn on Estimate', summary: { title: 'Turn on Estimate', lines: [access ? 'It says it will use the internet.' : 'It declares no capabilities.', 'Version 0.1.0. Runs on this computer.'], risk: 'An add-on runs its own code on this computer as you; turn it on only if you trust it.' }, fields_changed: [{ field: 'version', before: null, after: '0.1.0' }] })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('add-on review words', () => {
  it.each([[false, false], [false, true], [true, false], [true, true]])('shows the trust warning only for marketplace=%s with access=%s', (marketplace, access) => {
    const original = content(access)
    const result = addonContent(original, marketplace)
    expect(!!result.summary?.risk).toBe(marketplace && access)
    expect(result.summary?.lines.join(' ')).not.toContain('Version')
    expect(result.fields_changed).toEqual(original.fields_changed)
    if (!access) expect(result.summary?.lines).toEqual(["It can't open your files, terminals or the internet."])
  })
  it.each(['[]', '(none)', 'none', ''])('does not warn for an empty capability list: %s', capabilities => {
    const original = { ...content(true), fields_changed: [{ field: 'capabilities', before: null, after: capabilities }] }
    expect(addonContent(original, true).summary?.risk).toBeUndefined()
  })
  it('translates capability lists without changing signed values', () => {
    const original: SignContent = { headline: 'Turn on add-on', fields_changed: [{ field: 'capabilities', before: null, after: '[pty, network, spawn_agent]' }] }
    const result = addonContent(original, true)
    expect(result.summary?.lines).toEqual(['Access: [terminals, internet, agents]'])
    expect(result.summary?.risk).toContain('only if you trust it')
    expect(result.fields_changed).toEqual(original.fields_changed)
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
it('has no spinner or signature before confirmation, then reports signing', async () => {
  const sign = vi.spyOn(api, 'signClick').mockImplementation(() => new Promise(() => {}))
  const { container } = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><BuiltInAddOns workspace={{ id: 'ws' } as Workspace} rows={[{ id: 'estimate', title: 'Estimate', desc: '', state: 'off', locked: null, blocked: null, version: '0.1.0' }]} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('switch', { name: 'Estimate' }))
  const confirm = await screen.findByRole('button', { name: 'Turn on' })
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeVisible()
  expect(confirm.closest('[data-testid="addon-estimate-accordion"]')).not.toBeNull()
  expect(screen.getByText('Version').closest('details')).not.toHaveAttribute('open')
  expect(container.querySelector('.animate-spin')).toBeNull()
  expect(sign).not.toHaveBeenCalled()
  expect(screen.queryByText('Turning on…')).not.toBeInTheDocument()
  expect(screen.getByRole('switch')).not.toHaveAttribute('aria-busy')
  await waitFor(() => expect(confirm).toBeEnabled(), { timeout: 3000 })
  fireEvent.pointerDown(confirm)
  fireEvent.click(confirm)
  await waitFor(() => expect(sign).toHaveBeenCalledTimes(1))
  expect(screen.getByText('Turning on…')).toBeVisible()
}, 30000)
