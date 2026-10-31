import { effectiveState } from '@/api/shares'
import { openInfo } from '@/test/info'
import { QueryClient } from '@tanstack/react-query'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { ARM_MS } from '@/api/click'
import { setFeatures } from '@/api/features'
import { READS, clickOn, prepareByClick, show, useFakeHost, workspace } from '@/test/hostHarness'
import { queryClient } from '@/production/Shell'

// The production app on a fake server that speaks the Shares routes (Publish add-on). The link rule is tested here:
// shown once, gone after Done and after a reload, and in no cache, storage or log.
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const STAGED = 'ps_' + '1'.repeat(32)
const SID = 'c'.repeat(32)
const TOKEN = 'Zk3-token_SECRET0123456789abcdefghijklmnopqrs'
const LINK = `http://127.0.0.1:8104/s/${SID}#t=${TOKEN}`
const FILE = { name: 'report.md', size: 2048, sha256: 'ab'.repeat(32) }
const share = (over: Record<string, unknown> = {}) => ({
  id: SID, ticket: 'DEMO-0001', title: null, kind: 'secret', state: 'live', expires_at: '2099-10-25T08:00:00Z', view_limit: 0, views: 2,
  files: ['report.md'], created_at: '2026-10-18T08:00:00Z', server: 'http://127.0.0.1:8103', ...over,
})
let shares: unknown[] = []
let staged: unknown[] = []
let configured = true
let live = true
let listRefusal: { code: string } | null = null
let sendRefusal: { code: string; reason?: string } | null = null
let registerRefusal: { code: string; reason?: string } | null = null
let sendRefusalOnce: { code: string; reason?: string } | null = null
let uploadN = 0
let prepareRefusal: { code: string } | null = null
let ticketOwner: string | null | undefined
let workItems: { name: string; bytes: number }[] = []
let emptyResult = false
let onlyEmpty = false
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'publish'], addOns: { available: true, add_ons: [{ id: 'publish', title: 'Publish', desc: 'Share files.', state: 'on', locked: null, blocked: null }] } })])
const refusal = (r: { code: string; reason?: string }) =>
  Response.json({ ok: false, error: { code: r.code, message: r.code, retryable: false, ...(r.reason ? { detail: { reason: r.reason } } : {}) } }, { status: 409 })
let serverUp = true
const route = () => {
  serverUp = true
  shares = [share(), share({ id: 'd'.repeat(32), kind: 'once', view_limit: 3, views: 1, state: 'revoked' })]
  staged = []
  configured = true
  live = true
  listRefusal = null
  sendRefusal = null
  registerRefusal = null
  sendRefusalOnce = null
  uploadN = 0
  prepareRefusal = null
  ticketOwner = undefined
  workItems = []
  emptyResult = false
  onlyEmpty = false
  state.host.override = (path, method, body) => {
    if (path.endsWith('/tickets/DEMO-0001') && method === 'GET')
      return Response.json({ ...ticket, ...(ticketOwner === undefined ? {} : { people: { ...ticket.people, owner: ticketOwner } }), artifacts: [...(onlyEmpty ? [] : [{ name: 'report.md', kind: 'report', bytes: 2048, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }, { name: 'bad/name.md', kind: 'report', bytes: 1, sha256: 'cd'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }]), ...(emptyResult ? [{ name: 'T1-receipt.log', kind: 'log', bytes: 0, sha256: 'ee'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] : [])] })
    if (path.endsWith('/tickets/DEMO-0001/workdir') && method === 'GET')
      return Response.json({ available: true, exists: workItems.length > 0, codebase: false, folder: '.orch-work/DEMO-0001', files: workItems.length, bytes: 16, truncated: false, items: workItems })
    if (!path.includes('/shares')) return undefined
    if (method === 'GET' && listRefusal) return refusal(listRefusal)
    if (method === 'GET')
      return Response.json({ available: true, configured, server: configured ? 'http://127.0.0.1:8103' : null, share: configured ? 'http://127.0.0.1:8104' : null, workspace: A, owner: 'p_owner', live: configured && live && serverUp, shares: configured ? shares : [], staged })
    if (path.endsWith('/shares/prepare')) {
      if (prepareRefusal) return refusal(prepareRefusal)
      staged = [{ staged_id: STAGED, ticket: 'DEMO-0001', kind: body?.kind, days: body?.days, views: body?.views ?? 0, origin: 'person', files: [FILE], created_at: '2026-10-18T08:00:00Z' }]
      return Response.json({ staged: staged[0], server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8104' })
    }
    if (path.endsWith('/shares/upload')) {
      uploadN += 1
      return Response.json({ upload_id: 'pu_' + String(uploadN).repeat(32), name: body?.name, size: atob(String(body?.data)).length, sha256: 'ef'.repeat(32) })
    }
    if (path.endsWith('/shares/send')) {
      if (sendRefusal) return refusal(sendRefusal)
      if (sendRefusalOnce) {
        const r = sendRefusalOnce
        sendRefusalOnce = null
        return refusal(r)
      }
      staged = []
      shares = [...shares, share({ id: SID })]
      return Response.json({ share_id: SID, ticket: 'DEMO-0001', kind: 'secret', link: LINK, expires_at: '2099-10-25T08:00:00Z', view_limit: 0 })
    }
    if (path.endsWith('/shares/revoke')) {
      shares = shares.map((s) => ((s as { id: string }).id === body?.share_id ? { ...(s as object), state: 'revoked' } : s))
      return Response.json({ revoked: true })
    }
    if (path.endsWith('/shares/discard')) {
      staged = []
      return Response.json({ discarded: true })
    }
    if (path.endsWith('/shares/register')) return registerRefusal ? refusal(registerRefusal) : Response.json({ registered: true, result: 'registered' })
    return undefined
  }
}
const calls = (p: string) => state.host.calls.filter((c) => c.path.includes(p))
afterEach(() => sessionStorage.clear())

it('a show-once link opened as often as allowed reads Used up, not Live', () => {
  const base = { state: 'live' as const, expires_at: '2999-01-01T00:00:00Z', kind: 'once' as const, view_limit: 1 }
  expect(effectiveState({ ...base, views: 1 })).toBe('used')
  expect(effectiveState({ ...base, views: 0 })).toBe('live')
  expect(effectiveState({ ...base, kind: 'secret', views: 5 })).toBe('live')
  expect(effectiveState({ ...base, views: null })).toBe('live')
})

it('lists the shares: kind, end time, opens and state, with Stop sharing only on an active one; no link anywhere', async () => {
  route()
  show('/w/DEMO/shares')
  const table = await screen.findByRole('table', { name: 'Shares' }, T)
  const [live, revoked] = within(table).getAllByRole('row').slice(1)
  expect(live).toHaveTextContent('DEMO-0001')
  expect(live).toHaveTextContent('Anyone with the link')
  expect(live).toHaveTextContent('Active')
  expect(live).toHaveTextContent('2') // opens
  expect(within(live).getByRole('button', { name: /^Stop sharing/ })).toBeInTheDocument()
  expect(revoked).toHaveTextContent('Limited-view link · up to 3 views')
  expect(revoked).toHaveTextContent('1 of 3')
  expect(revoked).toHaveTextContent('Stopped')
  expect(within(revoked).queryByRole('button', { name: /^Stop sharing/ })).toBeNull()
  expect(document.body.textContent).not.toContain(TOKEN)
})

it('Publish on but no server set up: the Shares page says what is missing and where to set it, not "Could not load"', async () => {
  route()
  configured = false
  show('/w/DEMO/shares')
  const note = await screen.findByTestId('shares-not-configured', {}, T)
  expect(note).toHaveTextContent('Publish needs a publish server. Set it in Add-ons, Publish.')
  expect(within(note).getByRole('link', { name: /Add-ons, Publish/ })).toHaveAttribute('href', expect.stringContaining('/addons'))
  expect(screen.queryByText(/Could not load/)).toBeNull()
  expect(screen.queryByTestId('shares-empty')).toBeNull()
})

it('a share waiting for a signature hides "Nothing is shared yet", and an unreachable hub shows ONE plain banner', async () => {
  route()
  live = false
  staged = [{ staged_id: STAGED, ticket: 'DEMO-0001', kind: 'secret', days: 7, views: 0, origin: 'agent', files: [FILE], created_at: '2026-10-18T08:00:00Z' }]
  show('/w/DEMO/shares')
  const note = await screen.findByTestId('shares-unreachable', {}, T)
  expect(note).not.toHaveTextContent(/views and states|last knew/)
  expect(screen.queryByTestId('shares-empty')).toBeNull()
  expect(screen.getAllByTestId('shares-unreachable')).toHaveLength(1)
  expect(screen.queryByText('Sending')).toBeNull()
  staged = []
})

it('a server that is not running: the Shares page says sharing is not reachable and offers Try again, no terminal command', async () => {
  route()
  live = false
  show('/w/DEMO/shares')
  const note = await screen.findByTestId('shares-unreachable', {}, T)
  expect(note).toHaveTextContent('Sharing is paused: your hub cannot be reached')
  expect(note).not.toHaveTextContent('orch-publish')
  expect(within(note).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  expect(screen.getByRole('table', { name: 'Shares' })).toBeInTheDocument()
})

it('a failed load names the reason in plain words (here: no server set up), Settings keeps its Publish box', async () => {
  route()
  listRefusal = { code: 'publish.not_configured' }
  show('/w/DEMO/shares')
  const alert = await screen.findByRole('alert', {}, T)
  expect(alert).toHaveTextContent('No publish server is set up yet. Set one up on the Add-ons page.')
  expect(alert).not.toHaveTextContent('Something went wrong')
})

it('Stop sharing asks in plain words, posts once with an idem, and the row says Stopped', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/shares')
  await user.click(await screen.findByRole('button', { name: /^Stop sharing 1 file: report\.md, link ends/ }, T))
  expect(await screen.findByText('Stop sharing these files?')).toBeInTheDocument()
  expect(within(screen.getByRole('dialog')).getByText(/stops working at once/)).toBeInTheDocument()
  expect(calls('/shares/revoke')).toEqual([])
  await user.click(screen.getByRole('button', { name: 'Stop sharing' }))
  await waitFor(() => expect(calls('/shares/revoke')).toHaveLength(1), T)
  const body = calls('/shares/revoke')[0].body as Record<string, unknown>
  expect(body).toMatchObject({ share_id: SID })
  expect(body.idem).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
  await waitFor(() => expect(screen.queryByText('Stop sharing these files?')).not.toBeInTheDocument(), T)
  await waitFor(() => expect(screen.queryByRole('button', { name: /^Stop sharing 1 file: report\.md, link ends/ })).not.toBeInTheDocument(), T)
})

it('the sidebar lists Shares only while the add-on is on (the server adds the feature then)', async () => {
  route()
  show('/w/DEMO/worktrees')
  await screen.findByRole('navigation', { name: 'Main' }, T)
  expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Shares' })).toBeInTheDocument()
})

it('with the add-on off there is no Shares entry, no ticket box, and no call to the share routes', async () => {
  route()
  state.host.workspaces[0].features = [...READS]
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await screen.findByRole('navigation', { name: 'Main' }, T)
  await screen.findByRole('complementary', { name: 'Ticket details' }, T)
  expect(within(screen.getByRole('navigation', { name: 'Main' })).queryByRole('link', { name: 'Shares' })).toBeNull()
  expect(screen.queryByTestId('ticket-shares')).toBeNull()
  expect(calls('/shares')).toEqual([])
})

it('share files end to end: pick, prepare, read every file with its size, sign, then the link shows ONCE', async () => {
  route()
  clickOn(state.host.workspaces[0])
  window.innerWidth = 1400
  const user = userEvent.setup()
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const log = vi.spyOn(console, 'log')
  const info = vi.spyOn(console, 'info')
  const error = vi.spyOn(console, 'error')
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-shares', {}, T)
  expect(box).toHaveTextContent('Anyone with the link')
  await user.click(within(box).getByRole('button', { name: 'Share files' }))
  const form = await screen.findByRole('dialog', { name: 'Share files' }, T)
  expect(within(form).getByRole('checkbox', { name: /bad\/name\.md/ })).toBeDisabled()
  expect(form).toHaveTextContent('characters the server does not take')
  expect(within(form).getByRole('button', { name: 'Review selected files' })).toBeDisabled()
  await user.click(within(form).getByRole('checkbox', { name: /report\.md/ }))
  await user.selectOptions(within(form).getByLabelText('Works for'), '3')
  await user.click(within(form).getByRole('radio', { name: /Limited-view/ }))
  await user.selectOptions(within(form).getByLabelText('Up to'), '3')
  await user.click(within(form).getByRole('button', { name: 'Review selected files' }))
  await waitFor(() => expect(calls('/shares/prepare')).toHaveLength(1), T)
  expect(calls('/shares/prepare')[0].body).toMatchObject({ ticket: 'DEMO-0001', artifacts: ['report.md'], kind: 'once', days: 3, views: 3 })
  // the review before the signature lists every file with its size and a link to the ticket's artifacts
  const confirm = (await screen.findByRole('list', { name: 'Files to share' }, T)).closest('[role="dialog"]') as HTMLElement
  expect(screen.queryByRole('button', { name: 'Continue to approval' })).toBeNull() // r12-P11: one sheet, no extra step
  const files = within(confirm).getByRole('list', { name: 'Files to share' })
  expect(files).toHaveTextContent('report.md')
  expect(files).toHaveTextContent('2.0 KB')
  expect(within(files).getByRole('link', { name: 'Open report.md on the ticket' })).toHaveAttribute('href', expect.stringContaining('/ticket/DEMO-0001'))
  expect(calls('/shares/send')).toEqual([]) // nothing sent before the signature
  const sign = await readyToSign()
  expect(calls('/shares/send')).toEqual([]) // nothing sent before the signature
  expect(state.host.clickPrepares()).toHaveLength(1) // prepared on open, once; never armed (autoStart={false})
  expect(state.host.clickSigned).toEqual([]) // opened on the review, never armed or signed by itself: Sign is the person's press
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(state.host.clickSigned).toHaveLength(1)
  expect(calls('/shares/send')).toHaveLength(1)
  expect(calls('/shares/send')[0].body).toMatchObject({ staged_id: STAGED })
  expect((within(dialog).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  expect(within(dialog).getByRole('button', { name: /Copy/ })).toBeInTheDocument()
  expect(dialog).toHaveTextContent('You can copy this link only now.')
  expect(within(dialog).queryByRole('button', { name: 'Close' })).toBeNull() // no dead X: only the explicit button ends it
  // Escape does not lose it; only Done ends it
  await user.keyboard('{Escape}')
  expect(screen.getByRole('dialog', { name: 'Your link' })).toBeInTheDocument()
  // never written anywhere
  const mentions = (v: unknown) => JSON.stringify(v).includes(TOKEN)
  expect(setItem.mock.calls.some((c) => mentions(c))).toBe(false)
  expect(Object.values(localStorage).concat(Object.values(sessionStorage)).some((v) => v.includes(TOKEN))).toBe(false)
  expect([log, info, error].some((spy) => mentions(spy.mock.calls))).toBe(false)
  const cached = (qc: QueryClient) => JSON.stringify([...qc.getQueryCache().getAll().map((q) => q.state.data), ...qc.getMutationCache().getAll().map((m) => m.state.data)])
  expect(cached(queryClient)).not.toContain(TOKEN)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Your link' })).toBeNull(), T)
  expect(document.body.textContent).not.toContain(TOKEN)
  expect(cached(queryClient)).not.toContain(TOKEN)
  // the new share is listed, without its link
  await waitFor(() => expect(within(screen.getByTestId('ticket-shares')).getAllByRole('listitem').length).toBeGreaterThan(0), T)
  expect(document.body.textContent).not.toContain(TOKEN)
})

it('a reload shows the share in the list and never the link', async () => {
  route()
  show('/w/DEMO/shares')
  await screen.findByRole('table', { name: 'Shares' }, T)
  expect(document.body.textContent).not.toContain(TOKEN)
  expect(document.body.innerHTML).not.toContain('#t=')
})

it('cancelling at the review drops the prepared copy and sends nothing', async () => {
  route()
  clickOn(state.host.workspaces[0])
  window.innerWidth = 1400
  const user = userEvent.setup()
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(within(await screen.findByTestId('ticket-shares', {}, T)).getByRole('button', { name: 'Share files' }))
  const form = await screen.findByRole('dialog', { name: 'Share files' }, T)
  await user.click(within(form).getByRole('checkbox', { name: /report\.md/ }))
  await user.click(within(form).getByRole('button', { name: 'Review selected files' }))
  const confirm = (await screen.findByRole('list', { name: 'Files to share' }, T)).closest('[role="dialog"]') as HTMLElement
  await user.click(await within(confirm).findByRole('button', { name: 'Cancel' }, T))
  await waitFor(() => expect(calls('/shares/discard')).toHaveLength(1), T)
  expect(calls('/shares/send')).toEqual([])
  expect(calls('/handoff').filter((c) => c.method === 'POST')).toEqual([])
  expect(state.host.clickSigned).toEqual([]) // the review was prepared on open, nothing was signed
})

it('a server that does not know the workspace is said in words, with the admin step behind Technical details', async () => {
  route()
  clickOn(state.host.workspaces[0])
  sendRefusal = { code: 'publish.server_refused', reason: 'unknown_workspace' }
  window.innerWidth = 1400
  const user = userEvent.setup()
  staged = [{ staged_id: STAGED, ticket: 'DEMO-0001', kind: 'secret', days: 7, views: 0, origin: 'agent', files: [FILE], created_at: '2026-10-18T08:00:00Z' }]
  show('/w/DEMO/ticket/DEMO-0001')
  const box = await screen.findByTestId('ticket-shares', {}, T)
  expect(box).toHaveTextContent('Waiting for your signature')
  expect(box).toHaveTextContent('prepared by an agent')
  await user.click(within(box).getByRole('button', { name: 'Review and sign' }))
  ;(await readyToSign())()
  const alert = await screen.findByRole('alert', {}, { timeout: 9000 })
  expect(alert).toHaveTextContent('has not allowed this workspace to share yet')
  expect(alert).toHaveTextContent('Ask the hub’s admin')
  expect(alert).not.toHaveTextContent('publish.server_refused')
  expect(await openInfo()).toHaveTextContent(`orch-publish admin ws allow ${A} p_owner`)
})

it('Settings: the addresses in words, and the registration message when the server does not know the workspace', async () => {
  route()
  clickOn(state.host.workspaces[0])
  registerRefusal = { code: 'publish.server_refused', reason: 'unknown_workspace' }
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  await user.click(await screen.findByTestId('addon-publish-expand', {}, T))
  const box = await screen.findByTestId('publish-server', {}, T)
  expect(box).toHaveTextContent('http://127.0.0.1:8103')
  expect(box).toHaveTextContent('http://127.0.0.1:8104')
  await user.click(within(box).getByRole('button', { name: 'Check now' }))
  const reg = await screen.findByTestId('publish-registration', {}, T)
  await waitFor(() => expect(reg).toHaveTextContent('Ask the hub’s admin'), T)
  expect(reg).not.toHaveTextContent('publish.server_refused')
  expect((reg.textContent ?? '').match(/Ask the hub’s admin/g)).toHaveLength(1)
  expect(await openInfo(reg)).toHaveTextContent('orch-publish admin ws allow')
  // changing the addresses is signed with One click, with both addresses
  await user.click(within(box).getByRole('button', { name: 'Edit server' }))
  const dlg = await screen.findByRole('dialog', { name: 'Where shared files go' })
  await user.clear(within(dlg).getByLabelText('Publish server address'))
  await user.type(within(dlg).getByLabelText('Publish server address'), 'http://evil.example')
  expect(within(dlg).getByRole('alert')).toHaveTextContent('must start with https://')
  expect(within(dlg).getByRole('button', { name: 'Review and sign' })).toBeDisabled()
  await user.clear(within(dlg).getByLabelText('Publish server address'))
  await user.type(within(dlg).getByLabelText('Publish server address'), 'https://pub.example.org')
  // the saved addresses differ, so the second field is already open; equal addresses are accepted too
  await user.clear(within(dlg).getByLabelText('Links open at'))
  await user.type(within(dlg).getByLabelText('Links open at'), 'https://pub.example.org')
  expect(within(dlg).getByRole('button', { name: 'Review and sign' })).toBeEnabled()
  await user.clear(within(dlg).getByLabelText('Links open at'))
  await user.type(within(dlg).getByLabelText('Links open at'), 'https://apps.example.org')
  await user.click(within(dlg).getByRole('button', { name: 'Review and sign' }))
  const outcome = await screen.findByRole('button', { name: 'Choose where shared files go' }, T)
  await prepareByClick(outcome.closest('[role="dialog"]') as HTMLElement, 'Choose where shared files go')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'publish_setup', server: 'https://pub.example.org', share: 'https://apps.example.org' }])
  expect(calls('/handoff').filter((c) => c.method === 'POST')).toEqual([])
})

it('Settings: the Publish row says what it needs; Check connection is a read-only probe with honest states; Open Shares links on', async () => {
  route()
  const user = userEvent.setup()
  show('/w/DEMO/addons')
  const box = await openPublish()
  expect(within(box).getByText('Not checked.')).toBeInTheDocument()
  serverUp = false
  const before = state.host.calls.filter((c) => c.method !== 'GET').length
  await user.click(within(box).getByRole('button', { name: 'Check connection' }))
  await within(box).findByText(/Could not connect/, {}, T)
  serverUp = true
  await user.click(within(box).getByRole('button', { name: 'Check connection' }))
  await within(box).findByText(/Connected\. The server answered/, {}, T)
  expect(state.host.calls.filter((c) => c.method !== 'GET').length).toBe(before) // nothing was written
  expect(within(box).getByRole('link', { name: 'Open Shares' })).toHaveAttribute('href', '/w/DEMO/shares')
})

it('Settings: an unconfigured Publish says it needs a server address', async () => {
  route()
  configured = false
  show('/w/DEMO/addons')
  const box = await openPublish()
  expect(within(box).getByRole('button', { name: 'Set up' })).toBeInTheDocument()
})

/** Publish's settings open in place under its row. */
async function openPublish() {
  await userEvent.setup().click(await screen.findByTestId('addon-publish-expand', {}, T))
  return await screen.findByTestId('publish-server', {}, T)
}

/** The one sheet of a share (r12-P11): the review is prepared on open, Sign (labelled "Share files") is still the person's own
 * press; returns that press. */
async function readyToSign() {
  // the dialog is named by the host's own headline once the review has loaded, so find it by its Sign button
  const dialog = (await screen.findByRole('button', { name: 'Share files' }, T)).closest('[role="dialog"]') as HTMLElement
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'publish_share', key: 'DEMO-0001', staged_id: STAGED }])
  expect(calls('/handoff').filter((c) => c.method === 'POST')).toEqual([])
  const sign = within(dialog).getByRole('button', { name: 'Share files' })
  expect(state.host.clickSigned).toEqual([]) // never armed or signed before the person's own press (also on the resume path)
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 3000 })
  return () => {
    fireEvent.pointerDown(sign)
    fireEvent.click(sign)
  }
}

/** Share through to the armed signature on the ticket page; returns its press. */
async function signedShare(user: ReturnType<typeof userEvent.setup>) {
  clickOn(state.host.workspaces[0])
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(within(await screen.findByTestId('ticket-shares', {}, T)).getByRole('button', { name: 'Share files' }))
  const form = await screen.findByRole('dialog', { name: 'Share files' }, T)
  await user.click(within(form).getByRole('checkbox', { name: /report\.md/ }))
  await user.click(within(form).getByRole('button', { name: 'Review selected files' }))
  return readyToSign()
}

it('the link stays on screen until Done when the list cannot be read after the send', async () => {
  route()
  const user = userEvent.setup()
  const sign = await signedShare(user)
  const inner = state.host.override!
  state.host.override = (path, method, body) => {
    if (path.endsWith('/shares/send')) {
      // from now on the list fails (as a host that drops right after the send would)
      const ok = inner(path, method, body)
      const again = state.host.override
      state.host.override = (p, m, b) => (p.endsWith('/shares') && m === 'GET' ? Response.json({ ok: false, error: { code: 'host.unavailable', message: 'x', retryable: true } }, { status: 503 }) : inner(p, m, b))
      void again
      return ok
    }
    return inner(path, method, body)
  }
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['shares'] })
  })
  await waitFor(() => expect(queryClient.getQueryCache().findAll({ queryKey: ['shares'] }).some((q) => q.state.status === 'error')).toBe(true), T)
  expect((within(screen.getByRole('dialog', { name: 'Your link' })).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Your link' })).toBeNull(), T)
})

it('the link stays on screen until Done when the Publish feature flips off after the send', async () => {
  route()
  const user = userEvent.setup()
  ;(await signedShare(user))()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  const inner = state.host.override!
  state.host.override = (p, m, b) => (p.endsWith('/shares') && m === 'GET' ? Response.json({ available: false }) : inner(p, m, b))
  act(() => setFeatures({ features: [...READS], workspaces: { [A]: [...READS] } }))
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['shares'] })
  })
  await waitFor(() => expect(screen.queryByTestId('ticket-shares')).toBeNull(), T)
  expect((within(screen.getByRole('dialog', { name: 'Your link' })).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Your link' })).toBeNull(), T)
})

it('on the Shares page the link also stays until Done when the list then fails to load', async () => {
  route()
  clickOn(state.host.workspaces[0])
  staged = [{ staged_id: STAGED, ticket: 'DEMO-0001', kind: 'secret', days: 7, views: 0, origin: 'agent', files: [FILE], created_at: '2026-10-18T08:00:00Z' }]
  const user = userEvent.setup()
  show('/w/DEMO/shares')
  await user.click(await screen.findByRole('button', { name: 'Review and sign' }, T))
  const sign = await readyToSign()
  const inner = state.host.override!
  state.host.override = (p, m, b) => {
    const res = inner(p, m, b)
    if (p.endsWith('/shares/send')) state.host.override = (p2, m2, b2) => (p2.endsWith('/shares') && m2 === 'GET' ? Response.json({ ok: false, error: { code: 'host.unavailable', message: 'x', retryable: true } }, { status: 503 }) : inner(p2, m2, b2))
    return res
  }
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['shares'] })
  })
  await waitFor(() => expect(screen.queryByRole('table', { name: 'Shares' })).toBeNull(), T) // the body gave way to its load failure
  expect((within(screen.getByRole('dialog', { name: 'Your link' })).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
})

it('PL: the link dialog has no dead X, announces a copy, and the Settings note says a local link is local', async () => {
  route()
  show('/w/DEMO/addons')
  const box = await openPublish()
  expect(within(box).getByTestId('publish-local-note')).toHaveTextContent('opens only on this computer')
})

// ---- P05: a file picked on this computer, and connecting by itself

async function openForm(user: ReturnType<typeof userEvent.setup>) {
  clickOn(state.host.workspaces[0])
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(within(await screen.findByTestId('ticket-shares', {}, T)).getByRole('button', { name: 'Share files' }))
  return await screen.findByRole('dialog', { name: 'Share files' }, T)
}
const file = (name: string, text: string) => new File([text], name, { type: 'text/plain' })

it('Add a file: the pick is uploaded alone, listed with its size, enables Review, and Review names it with the artifacts', async () => {
  route()
  const user = userEvent.setup()
  const form = await openForm(user)
  expect(form).toHaveTextContent('Nothing is sent until you approve')
  expect(within(form).getByRole('button', { name: 'Review selected files' })).toBeDisabled()
  await user.upload(within(form).getByTestId('share-file-input'), file('readme.md', 'hello readme'))
  await waitFor(() => expect(calls('/shares/upload')).toHaveLength(1), T)
  const sent = calls('/shares/upload')[0].body as Record<string, unknown>
  expect(sent).toMatchObject({ name: 'readme.md', data: btoa('hello readme') })
  expect(sent.idem).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
  expect(await within(form).findByRole('checkbox', { name: 'readme.md from this computer' }, T)).toBeChecked()
  expect(form).toHaveTextContent('12 bytes')
  const review = within(form).getByRole('button', { name: 'Review selected files' })
  await waitFor(() => expect(review).toBeEnabled(), T)
  expect(calls('/shares/prepare')).toEqual([]) // nothing is prepared, let alone sent, by picking
  await user.click(within(form).getByRole('checkbox', { name: /report\.md/ }))
  await user.click(review)
  await waitFor(() => expect(calls('/shares/prepare')).toHaveLength(1), T)
  expect(calls('/shares/prepare')[0].body).toMatchObject({ ticket: 'DEMO-0001', artifacts: ['report.md'], uploads: ['pu_' + '1'.repeat(32)] })
  expect(calls('/shares/send')).toEqual([])
})

it('Add a file: Remove takes the pick out again, and Review is off with nothing selected', async () => {
  route()
  const user = userEvent.setup()
  const form = await openForm(user)
  await user.upload(within(form).getByTestId('share-file-input'), file('a.txt', 'abc'))
  await user.click(await within(form).findByRole('button', { name: 'Remove a.txt' }, T))
  expect(within(form).queryByRole('checkbox', { name: 'a.txt from this computer' })).toBeNull()
  expect(within(form).getByRole('button', { name: 'Review selected files' })).toBeDisabled()
})

it('Add a file: a file that is too big, empty, badly named or a double never leaves the browser', async () => {
  route()
  const user = userEvent.setup()
  const form = await openForm(user)
  const input = within(form).getByTestId('share-file-input')
  await user.upload(input, file('big.bin', 'x'.repeat(1024 * 1024 + 1)))
  expect(await within(form).findByRole('alert', {}, T)).toHaveTextContent('Larger than 1 MB')
  await user.upload(input, file('empty.txt', ''))
  await waitFor(() => expect(within(form).getByRole('alert')).toHaveTextContent('This file is empty'), T)
  await user.upload(input, file('.hidden', 'x'))
  await waitFor(() => expect(within(form).getByRole('alert')).toHaveTextContent('characters the server does not take'), T)
  await user.upload(input, file('REPORT.md', 'x'))
  await waitFor(() => expect(within(form).getByRole('alert')).toHaveTextContent('already in the list'), T)
  expect(calls('/shares/upload')).toEqual([])
})

it('a server that does not know the workspace is connected by itself and the send is retried once', async () => {
  route()
  sendRefusalOnce = { code: 'publish.server_refused', reason: 'unknown_workspace' }
  const user = userEvent.setup()
  const sign = await signedShare(user)
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(calls('/shares/register')).toHaveLength(1)
  expect(calls('/shares/send')).toHaveLength(2)
  expect((calls('/shares/send')[1].body as { idem: string }).idem).toBe((calls('/shares/send')[0].body as { idem: string }).idem)
  expect((within(dialog).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
})

it('when connecting is refused the first refusal is said in words and nothing loops', async () => {
  route()
  sendRefusal = { code: 'publish.server_refused', reason: 'unknown_workspace' }
  registerRefusal = { code: 'role.denied' }
  const user = userEvent.setup()
  ;(await signedShare(user))()
  const alert = await screen.findByRole('alert', {}, { timeout: 9000 })
  expect(alert).toHaveTextContent('has not allowed this workspace to share yet')
  expect(calls('/shares/register')).toHaveLength(1)
  expect(calls('/shares/send')).toHaveLength(1)
  expect(document.body.textContent).not.toMatch(/orch publish register|orch-publish dev/)
})

it('sharing not reachable: plain words, the signature stays good, Try again sends once more', async () => {
  route()
  sendRefusalOnce = { code: 'publish.unreachable' }
  const user = userEvent.setup()
  ;(await signedShare(user))()
  const alert = await screen.findByRole('alert', {}, { timeout: 9000 })
  expect(alert).toHaveTextContent('Sharing is paused: your hub cannot be reached. Your files were not sent.')
  expect(screen.getByRole('dialog')).toHaveTextContent('signature stays good for an hour')
  expect(document.body.textContent).not.toMatch(/orch-publish|terminal/i)
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(calls('/shares/send')).toHaveLength(2)
  expect(calls('/shares/register')).toEqual([])
})

// Run 14 P01: Publish as link on a result card: one button, one small panel, Sign and publish, the link once.
async function onResults(user: ReturnType<typeof userEvent.setup>) {
  state.host.workspaces[0].features = [...state.host.workspaces[0].features, 'actions']
  clickOn(state.host.workspaces[0])
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  await user.click(await screen.findByRole('tab', { name: /Results/ }, T))
  return await screen.findByRole('list', { name: 'Artifacts' }, T)
}

it('Publish as link: a button on the result card, a panel that names the file, Sign and publish, then the link shows ONCE', async () => {
  route()
  const user = userEvent.setup()
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const list = await onResults(user)
  // a file the server would not take has no button
  expect(within(list).queryByRole('button', { name: 'Publish bad/name.md as a link' })).toBeNull()
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const panel = await screen.findByRole('dialog', { name: 'Publish report.md as a link?' }, T)
  expect(panel).toHaveTextContent('Anyone with the link can open this file. Only this file leaves your computer.')
  expect(panel).toHaveTextContent('http://127.0.0.1:8103') // where it goes
  expect(calls('/shares/prepare')).toEqual([]) // nothing is prepared before the press
  await user.click(within(panel).getByRole('button', { name: 'Sign and publish' }))
  await waitFor(() => expect(calls('/shares/prepare')).toHaveLength(1), T)
  expect(calls('/shares/prepare')[0].body).toMatchObject({ ticket: 'DEMO-0001', artifacts: ['report.md'], kind: 'secret', days: 7 })
  expect(calls('/shares/send')).toEqual([]) // nothing leaves before the signature
  const sign = await readyToSign() // the existing click-to-sign dialog, no typed word
  expect(calls('/shares/send')).toEqual([])
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(state.host.clickSigned).toHaveLength(1)
  expect((within(dialog).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  expect(within(dialog).getByRole('button', { name: /Copy/ })).toBeInTheDocument()
  expect(dialog).toHaveTextContent('You can copy this link only now.')
  expect(setItem.mock.calls.some((c) => JSON.stringify(c).includes(TOKEN))).toBe(false)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Your link' })).toBeNull(), T)
  expect(document.body.textContent).not.toContain(TOKEN)
  expect(JSON.stringify([...queryClient.getQueryCache().getAll().map((q) => q.state.data), ...queryClient.getMutationCache().getAll().map((m) => m.state.data)])).not.toContain(TOKEN)
})

it('Publish as link: Cancel prepares nothing and sends nothing', async () => {
  route()
  const user = userEvent.setup()
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const panel = await screen.findByRole('dialog', { name: 'Publish report.md as a link?' }, T)
  await user.click(within(panel).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
  expect(calls('/shares/prepare')).toEqual([])
  expect(calls('/shares/send')).toEqual([])
})

it.each([
  ['hub.not_joined', /Connect this workspace to a hub in Settings, Hub/],
  ['hub.state_mismatch', /does not match what the workspace has signed/],
])('Publish as link: a hub refusal (%s) is said in plain words before anything is signed, never as a code', async (code, words) => {
  route()
  prepareRefusal = { code }
  const user = userEvent.setup()
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const panel = await screen.findByRole('dialog', { name: 'Publish report.md as a link?' }, T)
  await user.click(within(panel).getByRole('button', { name: 'Sign and publish' }))
  const alert = await within(panel).findByRole('alert', {}, T)
  expect(alert).toHaveTextContent(words)
  expect(alert).toHaveTextContent('Nothing was shared')
  expect(alert.textContent).not.toMatch(/\bhub\.[a-z_]+|state_mismatch|not_joined|selective/)
  expect(calls('/shares/send')).toEqual([])
  expect(state.host.clickPrepares()).toEqual([]) // no signature was asked for
})

it('Publish as link: a viewer sees no button', async () => {
  route()
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => (m.role === 'owner' ? { ...m, role: 'viewer' as const } : m))
  const user = userEvent.setup()
  const list = await onResults(user)
  expect(within(list).queryByRole('button', { name: /as a link/ })).toBeNull()
})

const asRole = (role: 'member' | 'maintainer') => {
  const ws = state.host.workspaces[0]
  ws.roster = ws.roster.map((m) => (m.person === ws.me ? { ...m, role } : m))
  return ws.me
}

it.each([
  ['a plain member on someone else\'s ticket', 'member', 'p_someone_else', false],
  ['the ticket owner (a member)', 'member', null, true],
  ['a maintainer on someone else\'s ticket', 'maintainer', 'p_someone_else', true],
] as const)('Publish as link: %s', async (_who, role, owner, sees) => {
  route()
  const user = userEvent.setup()
  const me = asRole(role)
  ticketOwner = owner ?? me
  const list = await onResults(user)
  const btn = within(list).queryByRole('button', { name: 'Publish report.md as a link' })
  if (sees) expect(btn).toBeInTheDocument()
  else expect(btn).toBeNull()
})


it('P03: the ticket\'s working files are offered next to the results; ticking one enables Review and prepare names it', async () => {
  route()
  workItems = [{ name: 'hello.txt', bytes: 16 }, { name: 'report.md', bytes: 5 }, { name: 'empty.txt', bytes: 0 }]
  emptyResult = true
  const user = userEvent.setup()
  const form = await openForm(user)
  const row = await within(form).findByRole('checkbox', { name: /hello\.txt/ }, T)
  expect(form).toHaveTextContent('16 bytes · working file')
  expect(within(form).getAllByRole('checkbox', { name: /report\.md/ })).toHaveLength(1) // a result of the same name is not offered twice
  expect(within(form).queryByRole('checkbox', { name: /empty\.txt|T1-receipt/ })).toBeNull()
  expect(form).toHaveTextContent('T1-receipt.log is empty, so it is left out.')
  expect(form).toHaveTextContent('empty.txt is empty, so it is left out.')
  const review = within(form).getByRole('button', { name: 'Review selected files' })
  expect(review).toBeDisabled()
  expect(form).toHaveTextContent('Pick a file first.')
  await user.click(row)
  expect(review).toBeEnabled()
  expect(form).toHaveTextContent('1 selected, 16 bytes.')
  await user.click(review)
  await waitFor(() => expect(calls('/shares/prepare')).toHaveLength(1), T)
  expect(calls('/shares/prepare')[0].body).toMatchObject({ ticket: 'DEMO-0001', artifacts: [], workfiles: ['hello.txt'] })
  expect(JSON.stringify(calls('/shares/prepare')[0].body)).not.toContain('orch-work') // a name, never a path
  expect(calls('/shares/send')).toEqual([])
})

it('P03: with only an empty result and no working files, the window says what to do', async () => {
  route()
  emptyResult = true
  onlyEmpty = true
  const user = userEvent.setup()
  const form = await openForm(user)
  expect(form).toHaveTextContent('Nothing is ready to share yet. Add a file from this computer.')
  expect(form).toHaveTextContent('Pick a file first.')
})
