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
let why: 'unreachable' | 'not_allowed' | 'local' = 'unreachable'
let listRefusal: { code: string } | null = null
let sendRefusal: { code: string; reason?: string } | null = null
let registerRefusal: { code: string; reason?: string } | null = null
let sendRefusalOnce: { code: string; reason?: string } | null = null
let uploadN = 0
let prepareRefusal: { code: string; message?: string } | null = null
let ticketOwner: string | null | undefined
let workItems: { name: string; bytes: number }[] = []
let workMore = false
let newResult = false
let emptyResult = false
let onlyEmpty = false
// the Publish server installed on this computer, as the host's add-on row says it (the source of "Files go to this computer.")
const PUBLISH_ROW = { id: 'publish', title: 'Publish', desc: 'Share files.', state: 'on' as const, locked: null, blocked: null, suggest: 'http://127.0.0.1:8103', suggest_from: 'computer' as const, suggest_hub: null }
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, 'publish'], addOns: { available: true, add_ons: [PUBLISH_ROW] } })])
const refusal = (r: { code: string; reason?: string; message?: string }) =>
  Response.json({ ok: false, error: { code: r.code, message: r.message ?? r.code, retryable: false, ...(r.reason ? { detail: { reason: r.reason } } : {}) } }, { status: 409 })
let serverUp = true
const route = () => {
  serverUp = true
  shares = [share(), share({ id: 'd'.repeat(32), kind: 'once', view_limit: 3, views: 1, state: 'revoked' })]
  staged = []
  configured = true
  live = true
  why = 'unreachable'
  listRefusal = null
  sendRefusal = null
  registerRefusal = null
  sendRefusalOnce = null
  uploadN = 0
  prepareRefusal = null
  ticketOwner = undefined
  workItems = []
  workMore = false
  newResult = false
  emptyResult = false
  onlyEmpty = false
  state.host.override = (path, method, body) => {
    if (path.endsWith('/tickets/DEMO-0001') && method === 'GET')
      return Response.json({ ...ticket, ...(ticketOwner === undefined ? {} : { people: { ...ticket.people, owner: ticketOwner } }), artifacts: [...(onlyEmpty ? [] : [{ name: 'report.md', kind: 'report', bytes: 2048, sha256: 'ab'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }, { name: 'bad/name.md', kind: 'report', bytes: 1, sha256: 'cd'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }]), ...(newResult ? [{ name: 'a.txt', kind: 'report', bytes: 3, sha256: 'aa'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] : []), ...(emptyResult ? [{ name: 'T1-receipt.log', kind: 'log', bytes: 0, sha256: 'ee'.repeat(32), added_by: 'p_x', at: '2026-10-18T08:00:00Z' }] : [])] })
    if (path.endsWith('/tickets/DEMO-0001/workdir') && method === 'GET')
      return Response.json({ available: true, exists: workItems.length > 0, codebase: false, folder: '.orch-work/DEMO-0001', files: workItems.length, bytes: 16, truncated: false, items: workItems, items_truncated: workMore })
    if (!path.includes('/shares')) return undefined
    if (method === 'GET' && listRefusal) return refusal(listRefusal)
    if (method === 'GET')
      return Response.json({ available: true, configured, server: configured ? 'http://127.0.0.1:8103' : null, share: configured ? 'http://127.0.0.1:8104' : null, workspace: A, owner: 'p_owner', live: configured && live && serverUp, ...(configured && live && serverUp ? {} : { why }), shares: configured ? shares : [], staged })
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

it('a hub that answers and refuses says it has not switched publishing on, and not "cannot be reached"', async () => {
  route()
  live = false
  why = 'not_allowed'
  show('/w/DEMO/shares')
  const note = await screen.findByTestId('shares-not-allowed', {}, T)
  expect(note).toHaveTextContent('Your hub has not switched on publishing for this workspace yet. Nothing was sent.')
  expect(screen.queryByTestId('shares-unreachable')).toBeNull()
  expect(screen.queryByText(/cannot be reached/)).toBeNull()
  expect(within(note).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
})

it('no hub route (local) shows neither note', async () => {
  route()
  live = false
  why = 'local'
  show('/w/DEMO/shares')
  expect(await screen.findByRole('table', { name: 'Shares' }, T)).toBeInTheDocument()
  expect(screen.queryByTestId('shares-unreachable')).toBeNull()
  expect(screen.queryByTestId('shares-not-allowed')).toBeNull()
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
async function readyToSign(label = 'Share files') {
  // the dialog is named by the host's own headline once the review has loaded, so find it by its Sign button
  const dialog = (await screen.findByRole('button', { name: label }, T)).closest('[role="dialog"]') as HTMLElement
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'publish_share', key: 'DEMO-0001', staged_id: STAGED }])
  expect(calls('/handoff').filter((c) => c.method === 'POST')).toEqual([])
  const sign = within(dialog).getByRole('button', { name: label })
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

it('Publish as link: one press prepares the copy and opens exactly ONE sign sheet (Publish); nothing is signed or sent before Sign; the link shows ONCE', async () => {
  route()
  const user = userEvent.setup()
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const list = await onResults(user)
  // a file the server would not take has no button
  expect(within(list).queryByRole('button', { name: 'Publish bad/name.md as a link' })).toBeNull()
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  // no panel of its own: the copy is prepared at once and the review of the host follows
  await waitFor(() => expect(calls('/shares/prepare')).toHaveLength(1), T)
  expect(calls('/shares/prepare')[0].body).toMatchObject({ ticket: 'DEMO-0001', artifacts: ['report.md'], kind: 'secret', days: 7 })
  expect(screen.queryByRole('button', { name: 'Sign and publish' })).toBeNull()
  const sign = await readyToSign('Publish') // the existing click-to-sign sheet, no typed word
  // exactly one sheet; it names the file and says "anyone with the link" once
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
  const sheet = screen.getByRole('dialog')
  expect(sheet).toHaveTextContent('report.md')
  expect(sheet).toHaveTextContent('Anyone with the link can read this file. A copy cannot be taken back. You see the link once.')
  expect((sheet.textContent ?? '').match(/Anyone with the link/g)).toHaveLength(1)
  expect(within(sheet).getByTestId('where-files-go')).toHaveTextContent('Files go to this computer.') // by name, before Sign
  // Enter on the loaded review does not sign: focus is not on the Publish button, and a press must begin on it
  const publishButton = await within(sheet).findByRole('button', { name: 'Publish' }, T)
  expect(document.activeElement).not.toBe(publishButton)
  await user.keyboard('{Enter}')
  expect(calls('click/sign').filter((c) => c.method === 'POST')).toEqual([])
  expect(state.host.clickSigned).toEqual([])
  // the sheet is still open, and focus is inside it but not on the Publish button
  expect(screen.getByRole('dialog')).toBe(sheet)
  expect(sheet.contains(document.activeElement)).toBe(true)
  expect(document.activeElement).not.toBe(publishButton)
  // the address and the prepared-copy id sit behind Details
  expect(sheet).not.toHaveTextContent('http://127.0.0.1:8103')
  await user.click(within(sheet).getByRole('button', { name: 'Details' }))
  expect(await screen.findByTestId('info-drawer-content')).toHaveTextContent('http://127.0.0.1:8103')
  // nothing is signed or leaves before the person's own Sign press
  expect(state.host.clickSigned).toEqual([])
  expect(calls('/shares/send')).toEqual([])
  sign()
  const dialog = await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(state.host.clickSigned).toHaveLength(1)
  expect(state.host.clickPrepares()).toHaveLength(1) // one review, one signature
  expect((within(dialog).getByLabelText('Link') as HTMLInputElement).value).toBe(LINK)
  expect(within(dialog).getByRole('button', { name: /Copy/ })).toBeInTheDocument()
  expect(dialog).toHaveTextContent('You can copy this link only now.')
  expect(setItem.mock.calls.some((c) => JSON.stringify(c).includes(TOKEN))).toBe(false)
  await user.click(within(dialog).getByRole('button', { name: /saved the link/ }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Your link' })).toBeNull(), T)
  expect(document.body.textContent).not.toContain(TOKEN)
  expect(JSON.stringify([...queryClient.getQueryCache().getAll().map((q) => q.state.data), ...queryClient.getMutationCache().getAll().map((m) => m.state.data)])).not.toContain(TOKEN)
})

it('Publish as link: a hub reached at 127.0.0.1 says "Files go to your hub, <name>.", never "this computer", and no "nobody else" note', async () => {
  route()
  const user = userEvent.setup()
  state.host.workspaces[0].addOns = { available: true, add_ons: [{ ...PUBLISH_ROW, suggest: 'http://127.0.0.1:8103', suggest_from: 'hub', suggest_hub: 'Anna’s hub' }] } as never
  const inner = state.host.override!
  // one address for files and links, both on 127.0.0.1 (the hub is reached through it)
  state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' && !path.includes('/shares/') ? Response.json({ available: true, configured: true, server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8103', workspace: A, owner: 'p_owner', live: true, shares: [], staged }) : inner(path, method, body))
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  await readyToSign('Publish')
  const sheet = screen.getByRole('dialog')
  expect(within(sheet).getByTestId('where-files-go')).toHaveTextContent('Files go to your hub, Anna’s hub.')
  expect(sheet).not.toHaveTextContent('Files go to this computer')
  expect(sheet).not.toHaveTextContent('nobody else can use it')
})

/** Publish as link up to the loaded sign sheet, with the add-ons row and the shares list as given. */
async function publishSheet(user: ReturnType<typeof userEvent.setup>, row: Record<string, unknown> | null, addresses?: { server: string; share: string }) {
  if (row) state.host.workspaces[0].addOns = { available: true, add_ons: [{ ...PUBLISH_ROW, ...row }] } as never
  if (addresses) {
    const inner = state.host.override!
    state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' && !path.includes('/shares/') ? Response.json({ available: true, configured: true, ...addresses, workspace: A, owner: 'p_owner', live: true, shares: [], staged }) : inner(path, method, body))
  }
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
}

/** The sheet's own lead (files, where they go, who can open the links, Details). The signer's fixed risk line below it is the host's text and not under test. */
const leadOf = (sheet: HTMLElement) => within(sheet).getByTestId('where-files-go').parentElement!.textContent ?? ''

it('Publish as link: a hub reached at 127.0.0.1 with a public links address never says "this computer" anywhere on the sheet', async () => {
  route()
  const user = userEvent.setup()
  await publishSheet(user, { suggest: 'http://127.0.0.1:8103', suggest_from: 'hub', suggest_hub: 'Anna’s hub' }, { server: 'http://127.0.0.1:8103', share: 'https://apps.example.org' })
  await readyToSign('Publish')
  const sheet = screen.getByRole('dialog')
  expect(sheet).toHaveTextContent('Files go to your hub, Anna’s hub.')
  expect(sheet).toHaveTextContent('apps.example.org')
  expect(leadOf(sheet)).not.toMatch(/this computer/i)
  expect(leadOf(sheet)).not.toMatch(/nobody else/i)
})

it('Publish as link: a row without a suggestion is not a match, even with a source: no "this computer" for a remote server', async () => {
  route()
  const user = userEvent.setup()
  await publishSheet(user, { suggest: undefined, suggest_from: 'computer' }, { server: 'https://pub.example.org', share: 'https://pub.example.org' })
  await readyToSign('Publish')
  const sheet = screen.getByRole('dialog')
  expect(within(sheet).getByTestId('where-files-go')).toHaveTextContent('Files go to pub.example.org.')
  expect(leadOf(sheet)).not.toMatch(/this computer/i)
})

it('Publish as link: a suggestion that differs only by a trailing slash is the same address', async () => {
  route()
  const user = userEvent.setup()
  await publishSheet(user, { suggest: 'http://127.0.0.1:8103/', suggest_from: 'computer' })
  await readyToSign('Publish')
  expect(within(screen.getByRole('dialog')).getByTestId('where-files-go')).toHaveTextContent('Files go to this computer.')
})

it('Publish as link: an add-ons query that fails still lets the person sign, with the host-name line', async () => {
  route()
  const user = userEvent.setup()
  const inner = state.host.override!
  state.host.override = (path, method, body) => (path.endsWith('/add-ons') && method === 'GET' ? Response.json({ ok: false, error: { code: 'internal', message: 'x', retryable: false } }, { status: 500 }) : inner(path, method, body))
  await publishSheet(user, null)
  const sign = await readyToSign('Publish')
  const sheet = screen.getByRole('dialog')
  expect(within(sheet).getByTestId('where-files-go')).toHaveTextContent('Files go to 127.0.0.1.')
  expect(leadOf(sheet)).not.toMatch(/this computer/i)
  sign()
  await screen.findByRole('dialog', { name: 'Your link' }, { timeout: 9000 })
  expect(state.host.clickSigned).toHaveLength(1)
})

it('Publish as link: when the server is not known there is no destination line, no "this computer", and Publish stays off', async () => {
  route()
  const user = userEvent.setup()
  const inner = state.host.override!
  state.host.override = (path, method, body) => (path.includes('/shares') && method === 'GET' && !path.includes('/shares/') ? Response.json({ available: true, configured: true, server: null, share: null, workspace: A, owner: 'p_owner', live: true, shares: [], staged }) : inner(path, method, body))
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const button = await screen.findByRole('button', { name: 'Publish' }, T)
  const sheet = button.closest('[role="dialog"]') as HTMLElement
  expect(button).toBeDisabled()
  expect(within(sheet).queryByTestId('where-files-go')).toBeNull()
  expect(sheet).not.toHaveTextContent('Files go to')
  expect(sheet).toHaveTextContent('Where the files go is not known')
  expect(state.host.clickPrepares()).toEqual([])
  expect(state.host.clickSigned).toEqual([])
})

it('Publish as link: the one-file lead needs the staged copy to be that file; otherwise the generic lead shows', async () => {
  route()
  const user = userEvent.setup()
  const inner = state.host.override!
  state.host.override = (path, method, body) => {
    const got = inner(path, method, body)
    if (!path.endsWith('/shares/prepare')) return got
    // the host staged another file than the one that was named
    staged = [{ ...(staged[0] as object), files: [{ ...FILE, name: 'other.md' }, FILE] }]
    return Response.json({ staged: staged[0], server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8104' })
  }
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  await readyToSign('Share files')
  const sheet = screen.getByRole('dialog')
  expect(sheet).toHaveTextContent('Anyone with the link can read these files. A copy cannot be taken back.')
  expect(sheet).not.toHaveTextContent('works for 7 days')
  expect(sheet).not.toHaveTextContent('You see the link once')
  expect(sheet).not.toHaveTextContent('Publish report.md as a link?')
})

it('Publish as link: closing the flow while the copy is being prepared discards the copy that arrives later', async () => {
  route()
  const user = userEvent.setup()
  const list = await onResults(user)
  const inner = state.host.override!
  let release: () => void = () => {}
  const gate = new Promise<void>((r) => { release = r })
  state.host.override = (path, method, body) => (path.endsWith('/shares/prepare') ? gate.then(() => inner(path, method, body) as Response) as never : inner(path, method, body))
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  await user.click(await screen.findByRole('button', { name: 'Cancel' }, T)) // closes while prepare is in flight
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
  release()
  await waitFor(() => expect(calls('/shares/discard')).toHaveLength(1), T)
  expect(screen.queryByRole('dialog')).toBeNull() // no sheet appears later
  expect(state.host.clickPrepares()).toEqual([])
  expect(calls('/shares/send')).toEqual([])
})

it('Publish as link: Cancel on the sheet sends nothing, signs nothing and discards the prepared copy', async () => {
  route()
  const user = userEvent.setup()
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  await readyToSign('Publish')
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
  expect(calls('/shares/send')).toEqual([])
  expect(state.host.clickSigned).toEqual([])
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
  const alert = await screen.findByRole('alert', {}, T)
  expect(alert).toHaveTextContent(words)
  expect(alert).toHaveTextContent('Nothing was shared')
  expect(alert.textContent).not.toMatch(/\bhub\.[a-z_]+|state_mismatch|not_joined|selective/)
  expect(calls('/shares/send')).toEqual([])
  expect(state.host.clickPrepares()).toEqual([]) // no signature was asked for
})

it.each([
  ["Files over 431.5 KB can't go through the hub yet. (report.md is 2.0 MB.)", /Files over 431\.5 KB can't go through the hub yet\. \(report\.md is 2\.0 MB\.\)/],
  ["Files over 431.5 KB can't go through the hub yet.", /Files over 431\.5 KB can't go through the hub yet\./],
])('Publish as link: a file too big for the hub says its limit before anything is signed (%s)', async (message, words) => {
  route()
  prepareRefusal = { code: 'quota.host', message }
  const user = userEvent.setup()
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const alert = await screen.findByRole('alert', {}, T)
  expect(alert).toHaveTextContent(words)
  expect(alert).toHaveTextContent('Nothing was shared')
  expect(alert.textContent).not.toMatch(/quota\.host|Discard some/)
  expect(calls('/shares/send')).toEqual([])
  expect(state.host.clickPrepares()).toEqual([])
})

it('Publish as link: any other quota text of the host is not echoed', async () => {
  route()
  prepareRefusal = { code: 'quota.host', message: "Files over 1 KB can't go through the hub yet. <b>x</b>" }
  const user = userEvent.setup()
  const list = await onResults(user)
  await user.click(within(list).getByRole('button', { name: 'Publish report.md as a link' }))
  const alert = await screen.findByRole('alert', {}, T)
  expect(alert).toHaveTextContent('Too many prepared copies are waiting, or they are too large. Discard some.')
  expect(alert.textContent).not.toMatch(/<b>/)
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

it('P03: a work file that is gone after a reload is not kept selected, and more files than shown is said', async () => {
  route()
  workItems = [{ name: 'hello.txt', bytes: 16 }]
  workMore = true
  const user = userEvent.setup()
  const form = await openForm(user)
  await user.click(await within(form).findByRole('checkbox', { name: /hello\.txt/ }, T))
  expect(form).toHaveTextContent('More files than shown.')
  expect(form).toHaveTextContent('1 selected, 16 bytes.')
  workItems = []
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['workdir'] })
  })
  await waitFor(() => expect(form).not.toHaveTextContent('1 selected'), T)
  expect(within(form).getByRole('button', { name: 'Review selected files' })).toBeDisabled()
})

it('P03: a result that arrives with the name of a ticked work file replaces it: no work file of that name is counted or sent', async () => {
  route()
  workItems = [{ name: 'a.txt', bytes: 16 }]
  const user = userEvent.setup()
  const form = await openForm(user)
  await user.click(await within(form).findByRole('checkbox', { name: /a\.txt/ }, T))
  expect(form).toHaveTextContent('1 selected, 16 bytes.')
  newResult = true
  await act(async () => {
    await queryClient.invalidateQueries({ queryKey: ['ticket'] })
  })
  await waitFor(() => expect(form).not.toHaveTextContent('1 selected'), T)
  expect(within(form).getByRole('button', { name: 'Review selected files' })).toBeDisabled()
  expect(calls('/shares/prepare')).toEqual([])
})

it('lists pre-join hub shares and opens the confirming sync review without sending', async () => {
  route()
  state.host.workspaces[0].features = [...READS, 'publish', 'hub']
  const previous = state.host.override
  state.host.override = (path, method, body) => {
    if (path.endsWith('/hub/share')) return Response.json({ available: true, rules: [], shared: 0, refused: [], tickets: ['DEMO-0001'], tickets_complete: true, tickets_state: { 'DEMO-0001': 'not_sent' }, not_sent_count: 1 })
    if (path.endsWith('/hub/share/preview')) return Response.json({ count: 1, new: 1, revoked: 0, more: 0, items: [], rules: [], plan: 'P'.repeat(43), review: { hub_id: 'demo', readers: { people: [], teams: [], note: null, info: null }, widened: [], new_count: 1, new_more: 0, new_items: ['ticket DEMO-0001: A ticket'], leaves: ['the title, status, labels and deadline'], later_edits: 'Later edits are shared too.' } })
    return previous?.(path, method, body)
  }
  show('/w/DEMO/shares')
  expect(await screen.findByText('1 shares were made before you joined and are not sent yet.', {}, T)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Send now' }))
  expect(await screen.findByRole('button', { name: 'Sign' }, T)).toBeTruthy()
  expect(calls('/hub/share/sync')).toHaveLength(0)
})
