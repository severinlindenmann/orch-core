// Settings → Devices, "Pair a phone" (proposed pair/1, fixtures only): the gate, the blocked reasons, the link's
// secrecy and lifetime, the typed SAS with Reject as the default, untrusted labels and the device remove hand-off.
import { act, cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import { checkSas, pairingApi, pairServed, realPairingApi } from '@/api/pairing'
import { pairPreviewForTests, resetLayoutPreview } from '@/dev/layoutPreview'
import { createPairFixture, installPairFixture } from '@/dev/pairFixture'
import { renderApp } from '@/test/renderApp'

const SECRET = 'demo-not-a-real-pair-secret'
const T = { timeout: 4000 }

let consoleCalls: unknown[][] = []
beforeEach(() => {
  consoleCalls = []
  for (const k of ['log', 'info', 'warn', 'error', 'debug', 'trace'] as const) vi.spyOn(console, k).mockImplementation((...a: unknown[]) => void consoleCalls.push(a))
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  resetLayoutPreview()
  setFeatures(null)
})

const open = (scenario: string, opts?: Parameters<typeof pairPreviewForTests.set>[0]) => {
  pairPreviewForTests.set(opts ?? null)
  return renderApp(`/__layout/${scenario}`)
}

/** Everything the page could have leaked the link into. */
function expectNoLinkAnywhere(view: ReturnType<typeof renderApp>) {
  const caches = JSON.stringify([
    view.client.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]),
    view.client.getMutationCache().getAll().map((m) => m.state.data),
  ])
  expect(caches).not.toContain(SECRET)
  for (const store of [localStorage, sessionStorage]) for (let i = 0; i < store.length; i++) expect(String(store.getItem(store.key(i)!))).not.toContain(SECRET)
  expect(window.location.href).not.toContain(SECRET)
  expect(view.address()).not.toContain(SECRET)
  expect(JSON.stringify(window.history.state ?? null)).not.toContain(SECRET)
  expect(JSON.stringify(consoleCalls.map((c) => c.map(String)))).not.toContain(SECRET)
}

describe('the gate', () => {
  it('the real api answers "not available" and the gate needs both the feature and a served api', async () => {
    expect(realPairingApi.served('w')).toBe(false)
    await expect(realPairingApi.offer('w')).rejects.toMatchObject({ code: 'pair.unavailable' })
    setFeatures({ features: [] })
    expect(pairServed('w')).toBe(false)
    setFeatures({ features: ['pair'] })
    expect(pairServed('w')).toBe(false) // the real implementation: not until the ops are agreed
    const undo = installPairFixture(createPairFixture())
    expect(pairServed('w')).toBe(true)
    setFeatures({ features: [] })
    expect(pairServed('w')).toBe(false)
    undo()
    expect(pairingApi.served).toBe(realPairingApi.served)
  })

  it('pair-unavailable says so and calls nothing', async () => {
    open('pair-unavailable')
    expect(await screen.findByTestId('pair-unavailable', {}, T)).toHaveTextContent('Pairing a phone is not available on this host yet.')
    expect(pairPreviewForTests.fixture()!.calls).toEqual([])
  })
})

describe('before offering', () => {
  it('convenience on but never used: the turn-off command, no Create button', async () => {
    open('pair-blocked-convenience')
    const blocked = await screen.findByTestId('pair-blocked', {}, T)
    expect(blocked).toHaveAttribute('data-code', 'sign.convenience_readers')
    expect(blocked).toHaveTextContent('click-to-sign is on, or was used, on this workspace')
    expect(within(blocked).getByLabelText('Command to turn click-to-sign off')).toHaveTextContent('orch host convenience off --workspace DEMO')
    expect(blocked).not.toHaveTextContent("can't get a phone")
    expect(screen.queryByRole('button', { name: 'Create pairing link' })).toBeNull()
  })

  it('convenience used: no command, a workspace that used click-to-sign cannot get a phone yet', async () => {
    open('pair-blocked-convenience', { preview: { blocked: { code: 'sign.convenience_readers', message: 'click-to-sign was used here', history: true } } })
    const blocked = await screen.findByTestId('pair-blocked', {}, T)
    expect(blocked).toHaveTextContent("A workspace that used click-to-sign can't get a phone yet")
    expect(within(blocked).queryByLabelText('Command to turn click-to-sign off')).toBeNull()
  })

  it('convenience, history unknown: both cases are explained', async () => {
    open('pair-blocked-convenience', { preview: { blocked: { code: 'sign.convenience_readers', message: 'no' } } })
    const blocked = await screen.findByTestId('pair-blocked', {}, T)
    expect(blocked).toHaveTextContent('If click-to-sign was never used here')
    expect(blocked).toHaveTextContent('If it was used here')
  })

  it.each([
    ['relay.not_linked', 'this workspace has no relay link'],
    ['relay.quota', 'the relay account is full'],
    ['host.limit', 'too many devices'],
    ['keys.primary_missing', 'run orch keys primary init first'],
  ])('blocked %s shows its code and the host message, and nothing to create', async (code, message) => {
    open('pair-offer', { preview: { blocked: { code, message } } })
    const blocked = await screen.findByTestId('pair-blocked', {}, T)
    expect(blocked).toHaveTextContent(code)
    expect(blocked).toHaveTextContent(message)
    expect(screen.queryByTestId('pair-blocked-convenience')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Create pairing link' })).toBeNull()
  })

  it('warnings show before Create pairing link', async () => {
    open('pair-offer')
    const warnings = await screen.findByTestId('pair-warnings', {}, T)
    expect(warnings).toHaveTextContent('Click-to-sign stops working for this workspace once a phone is paired.')
    expect(screen.getByRole('button', { name: 'Create pairing link' })).toBeEnabled()
    expect(pairPreviewForTests.fixture()!.calls.some((c) => c.op === 'offer')).toBe(false)
  })
})

describe('the offer and its link', () => {
  async function created(opts?: Parameters<typeof pairPreviewForTests.set>[0]) {
    const view = open('pair-offer', opts)
    await view.user.click(await screen.findByRole('button', { name: 'Create pairing link' }, T))
    await screen.findByTestId('pair-waiting', {}, T)
    return view
  }

  it('the QR waits for Show QR; the fallback is selectable text, never a link; Copy link copies the native link', async () => {
    const view = await created()
    expect(screen.queryByTestId('pair-qr')).toBeNull()
    await view.user.click(screen.getByRole('button', { name: 'Show QR' }))
    expect(screen.getByTestId('pair-qr').tagName.toLowerCase()).toBe('svg')
    const fallback = screen.getByTestId('pair-fallback')
    expect(fallback.textContent).toMatch(/^https:\/\/relay\.example\.test\/pair#v2\./)
    expect(fallback.closest('a')).toBeNull()
    expect(document.querySelectorAll('a[href*="pair"]').length).toBe(0)
    expect(document.querySelectorAll('[href*="' + SECRET + '"]').length).toBe(0)
    await view.user.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(await navigator.clipboard.readText()).toMatch(/^orch:\/\/pair\?relay=.*#v2\./)
    expect(screen.getByTestId('pair-countdown').textContent).toMatch(/^[45]:\d\d$/)
    expectNoLinkAnywhere(view)
  })

  it('the link is dropped at the first pending, and the person types the phone code', async () => {
    const view = await created()
    expect(document.body.innerHTML).toContain(SECRET)
    const fixture = pairPreviewForTests.fixture()!
    act(() => fixture.emit(fixture.offers()[0].offer_id, 'pending', { sas: 'K7Q2MA', label: 'Phone' }))
    await screen.findByTestId('pair-compare', {}, T)
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(document.body.textContent).not.toContain('K7Q2MA') // the host's SAS is compared, never shown
    expectNoLinkAnywhere(view)
  })

  it('the link is dropped at expiry (the host expires_ms, not a fixed time)', async () => {
    const view = await created({ ttlMs: 400 })
    expect(document.body.innerHTML).toContain(SECRET)
    expect(await screen.findByTestId('pair-end-expired', {}, T)).toHaveTextContent('The pairing link expired.')
    expect(document.body.innerHTML).not.toContain(SECRET)
    expectNoLinkAnywhere(view)
  })

  it('cancel drops the link and cancels the offer', async () => {
    const view = await created()
    await view.user.click(screen.getByRole('button', { name: 'Cancel pairing' }))
    expect(await screen.findByTestId('pair-end-cancelled', {}, T)).toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(pairPreviewForTests.fixture()!.calls.filter((c) => c.op === 'cancel')).toHaveLength(1)
    expectNoLinkAnywhere(view)
  })

  it('unmount drops the link', async () => {
    const view = await created()
    expectNoLinkAnywhere(view)
    view.unmount()
    expect(document.body.innerHTML).not.toContain(SECRET)
    expect(pairPreviewForTests.fixture()!.calls.filter((c) => c.op === 'offer')).toHaveLength(1)
  })

  it('an open offer whose link this page does not hold: cancel and make a new one', async () => {
    open('pair-offer', { offers: [{ offer_id: 'of_x', state: 'open', created_ms: 0, expires_ms: Date.now() + 60_000, scopes: ['look'] }] })
    expect(await screen.findByTestId('pair-lost-link', {}, T)).toHaveTextContent("can't show it again")
    expect(pairPreviewForTests.fixture()!.calls.some((c) => c.op === 'offer')).toBe(false)
  })
})

describe('compare the code', () => {
  it('Reject is the default and focused; Approve only after the typed code matches', async () => {
    const { user } = open('pair-pending')
    await screen.findByTestId('pair-compare', {}, T)
    const reject = screen.getByRole('button', { name: 'Reject' })
    const approve = screen.getByRole('button', { name: 'Approve' })
    await waitFor(() => expect(reject).toHaveFocus())
    expect(approve).toBeDisabled()
    const input = screen.getByLabelText('Type the six characters your phone shows')
    await user.type(input, 'K7Q2MB')
    expect(screen.getByTestId('pair-sas-differ')).toHaveTextContent('The codes differ: reject this phone.')
    expect(approve).toBeDisabled()
    await user.type(input, '{Enter}')
    expect(pairPreviewForTests.fixture()!.calls.some((c) => c.op === 'answer')).toBe(false)
    await user.clear(input)
    await user.type(input, 'k7q-2ma')
    expect(approve).toBeEnabled()
    await user.click(approve)
    await waitFor(() => expect(pairPreviewForTests.fixture()!.calls.find((c) => c.op === 'answer')?.args).toEqual(['of_' + '7'.repeat(32), true]))
  })

  it('Reject answers no', async () => {
    const { user } = open('pair-pending')
    await user.click(await screen.findByRole('button', { name: 'Reject' }, T))
    await waitFor(() => expect(pairPreviewForTests.fixture()!.calls.find((c) => c.op === 'answer')?.args).toEqual(['of_' + '7'.repeat(32), false]))
    expect(await screen.findByTestId('pair-end-rejected', {}, T)).toHaveTextContent('Rejected on this Mac.')
  })

  it('checkSas: base32, case-insensitive, spaces and dashes ignored', () => {
    expect(checkSas('', 'K7Q2MA')).toBe('empty')
    expect(checkSas('k7q', 'K7Q2MA')).toBe('partial')
    expect(checkSas('k7 q2-ma', 'K7Q2MA')).toBe('match')
    expect(checkSas('K7Q2MB', 'K7Q2MA')).toBe('differ')
    expect(checkSas('K7Q2MA', null)).toBe('differ')
  })

  it('the device label is rendered as text, isolated, never as markup', async () => {
    const label = '<img src=x onerror=alert(1)>‮evil'
    open('pair-pending', { offers: [{ offer_id: 'of_l', state: 'pending', created_ms: 0, expires_ms: Date.now() + 60_000, scopes: ['look'], sas: 'AAAAAA', label }] })
    const shown = await screen.findByTestId('pair-label', {}, T)
    expect(shown.querySelector('img')).toBeNull()
    expect(shown.querySelector('bdi')).not.toBeNull()
    expect(shown).toHaveTextContent('<img src=x onerror=alert(1)>')
    expect(shown).toHaveTextContent('named by the device')
  })
})

describe('after the code', () => {
  it('cert_pending shows the cert code and the primary command as text', async () => {
    open('pair-cert')
    expect(await screen.findByTestId('pair-cert-code', {}, T)).toHaveTextContent('H4WZ7P')
    expect(screen.getByTestId('pair-primary-command')).toHaveTextContent('orch keys primary approve')
    expect(screen.getByTestId('pair-primary-command').closest('a')).toBeNull()
  })

  it('with the certificate in, Add the phone opens the hand-off for relay.pair_device', async () => {
    const { user } = open('pair-cert', { offers: [{ offer_id: 'of_c', state: 'cert_pending', created_ms: 0, expires_ms: Date.now() + 60_000, scopes: ['look'], cert_ready: true }] })
    const add = await screen.findByRole('button', { name: 'Add the phone' }, T)
    const spy = vi.spyOn(api, 'prepareHandoff')
    await user.click(add)
    await screen.findByRole('dialog', {}, T)
    expect(spy.mock.calls[0][1]).toEqual({ action: 'pair_device', offer: 'of_c' })
    spy.mockRestore()
  })

  it('failed shows the reason and the remove-the-device text', async () => {
    open('pair-failed')
    const end = await screen.findByTestId('pair-end-failed', {}, T)
    expect(end).toHaveTextContent('the certificate did not verify')
    expect(screen.getByTestId('pair-needs-remove')).toHaveTextContent('remove the device')
  })
})

describe('paired devices', () => {
  it('lists label, relay, reader; Remove opens the device.remove hand-off that rotates the relay keys', async () => {
    const { user } = open('pair-devices')
    const rows = await screen.findAllByTestId('device-row', {}, T)
    const spy = vi.spyOn(api, 'prepareHandoff')
    expect(rows.length).toBe(10)
    expect(rows[1]).toHaveTextContent('Phone 2')
    expect(rows[1]).toHaveTextContent('no')
    await user.click(within(rows[1]).getByRole('button', { name: 'Remove' }))
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).getByTestId('device-remove-rotates')).toHaveTextContent('This also rotates the relay keys.')
    expect(spy.mock.calls[0][1]).toEqual({ action: 'device_remove', device: 'd_' + '2'.repeat(32) })
    spy.mockRestore()
  })
})
