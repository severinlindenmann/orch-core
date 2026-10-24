import { act, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import { invalidateLive, useLiveUpdates } from '@/api/useLiveUpdates'
import board from '@/test/fixtures/server-m2/board-page.json'
import changes from '@/test/fixtures/server-m2/changes.json'
import events from '@/test/fixtures/server-m2/events.json'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import today from '@/test/fixtures/server-m2/today.json'
import { queryClient, Shell } from './Shell'

// Every page of the app reads through the production api, as the build wires it (vite.config.ts alias).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ws = 'a'.repeat(32)
const person = 'p_user1'
const FEATURES = ['identity', 'today', 'tickets', 'board', 'ticket', 'changes', 'evidence']
const workspaces = [{ id: ws, prefix: 'DEMO', name: 'Dev workspace', members: [{ person, name: 'Severin', role: 'owner', devices: 1 }], needs_you: 2, host_state: 'ok', features: FEATURES }]
const me = [{ person, name: 'Severin', role: 'owner', tier: 'A', workspace: ws }]
const empty = { rows: [], next_page: null, total: 0, sub_cursor: 'c', as_of: '2026-10-11T09:00:00Z' }
let ticketDoc: unknown
let login: string | undefined = 'local'

function route(url: string): unknown {
  const [path] = url.split('?')
  if (path === '/api/session') return { features: FEATURES, workspaces: { [ws]: FEATURES }, ...(login ? { login } : {}) }
  if (path === '/api/workspaces') return workspaces
  if (path === '/api/me') return me
  if (path === `/api/w/${ws}/today`) return today
  if (path === `/api/w/${ws}/board` || path === `/api/w/${ws}/tickets`) return url.includes('page=') ? { ...empty, rows: [] } : { ...board, next_page: null }
  if (path === `/api/w/${ws}/tickets/DEMO-0001`) return ticketDoc
  if (path === `/api/w/${ws}/tickets/DEMO-0001/events`) return events
  if (path === `/api/w/${ws}/tickets/DEMO-0001/changes`) return changes
  return undefined
}

beforeEach(() => {
  ticketDoc = ticket
  login = 'local'
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = route(url)
    return body === undefined ? Response.json({ ok: false, error: { code: 'not_found', message: 'No such route in the test', retryable: false } }, { status: 404 }) : Response.json(body)
  }))
  localStorage.clear()
})
afterEach(() => {
  // A toast lives in sonner's module state, not in the rendered tree: without this it shows in the next test's page.
  toast.dismiss()
  setFeatures(null)
  queryClient.clear()
  vi.unstubAllGlobals()
})

function show(path: string) {
  history.replaceState(null, '', path)
  return render(<QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>)
}
const methods = () => vi.mocked(fetch).mock.calls.map((c) => (c[1] as RequestInit | undefined)?.method)
const nav = () => within(screen.getByRole('navigation', { name: 'Main' }))

it('Today lists what needs the viewer from the host, without any button that signs', async () => {
  show('/w/DEMO')
  expect(await screen.findByText('Approve the requirements')).toBeInTheDocument()
  expect(screen.getAllByText('Real P1 ticket').length).toBeGreaterThan(0)
  expect(screen.getAllByText('Answer in your terminal').length).toBe(2)
  for (const name of ['Review', 'Answer', 'Give verdict', 'Send answer', 'New ticket']) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
  // The mockup's own demo and viewer controls do not exist in production.
  expect(screen.queryByTestId('demo-mode')).not.toBeInTheDocument()
  expect(screen.queryByText(/viewing as/i)).not.toBeInTheDocument()
  // A session opened from this computer has nothing to log out of.
  expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
  expect(new Set(methods())).toEqual(new Set(['GET']))
})

it.each([['hub', true], ['unknown', false], [undefined, false]])('Log out for a %s session: shown %s', async (kind, shown) => {
  login = kind
  show('/w/DEMO')
  expect(await screen.findByText('Approve the requirements')).toBeInTheDocument()
  if (shown) expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()
  else expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
})

it('only the advertised pages are in the navigation', async () => {
  show('/w/DEMO')
  await screen.findByText('Approve the requirements')
  expect(nav().getAllByRole('link').map((a) => a.textContent)).toEqual(expect.arrayContaining(['Today', 'Board', 'Tickets']))
  for (const name of ['Artifacts', 'Agents']) expect(nav().queryByRole('link', { name })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
})

it('an address of a page the host does not serve leads to the board', async () => {
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('heading', { name: /board/i }).catch(() => null) ?? await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  await waitFor(() => expect(location.pathname).toBe('/w/DEMO/board'))
})

it('the board draws the host\'s tickets and cannot move them', async () => {
  show('/w/DEMO/board')
  expect(await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  expect(screen.getByText('Second')).toBeInTheDocument()
  expect(new Set(methods())).toEqual(new Set(['GET']))
  expect(vi.mocked(fetch).mock.calls.some((c) => String(c[0]).includes(`/api/w/${ws}/board`))).toBe(true)
})

it('the ticket page shows sections, tasks, questions and the log from the host', async () => {
  const user = userEvent.setup()
  ticketDoc = { ...ticket, status: 'in-progress' }
  show('/w/DEMO/ticket/DEMO-0001')
  expect(await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  expect(screen.getAllByText('req').length).toBeGreaterThan(0)
  await user.click(screen.getByRole('tab', { name: /Acceptance/ }))
  expect(await screen.findByText('Write the export')).toBeInTheDocument()
  await user.click(screen.getByRole('tab', { name: /Questions/ }))
  expect(await screen.findByText('Any naming rule?')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /send answer|answer/i })).not.toBeInTheDocument()
  await user.click(screen.getByRole('tab', { name: 'History' }))
  expect(await screen.findByText(/AC1 added/)).toBeInTheDocument()
  expect(screen.getAllByText('checked by the host').length).toBeGreaterThan(0)
  await user.click(screen.getByRole('tab', { name: /Changes/ }))
  expect(await screen.findByLabelText('Diff')).toBeInTheDocument()
  expect(screen.getAllByText('a.txt').length).toBeGreaterThan(0)
  expect(screen.getByText('+two')).toBeInTheDocument()
  await user.click(screen.getByRole('tab', { name: 'Raw' }))
  for (const name of [/approve/i, /request changes/i, /verdict/i]) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
  expect(new Set(methods())).toEqual(new Set(['GET']))
})

it('a ticket that is no longer visible closes the open ticket view and drops its cache', async () => {
  show('/w/DEMO/ticket/DEMO-0001')
  expect(await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  await waitFor(() => expect(vi.mocked(useLiveUpdates).mock.calls.at(-1)?.[0]).toBe(ws))
  const close = vi.mocked(useLiveUpdates).mock.calls.at(-1)![1]!
  const uid = (ticket as { uid: string }).uid
  expect(queryClient.getQueryData(['ticket', ws, 'DEMO-0001'])).toBeTruthy()
  ticketDoc = undefined // the host now answers not_found, as it does for a ticket the person may not see
  act(() => invalidateLive(queryClient, ws, 'items', [{ id: '9.g', uid, gone: true }], close))
  await waitFor(() => expect(location.pathname).toBe('/w/DEMO/board'))
  expect(queryClient.getQueryData(['ticket', ws, 'DEMO-0001'])).toBeUndefined()
  expect(screen.getByText('DEMO-0001 is no longer visible to you')).toBeInTheDocument()
})

it('parent and blocked_by are plain text unless the key is a cached visible row; unknown keys cause no read', async () => {
  ticketDoc = { ...(ticket as object), parent: 'DEMO-0099', blocked_by: ['DEMO-0098', 'DEMO-0097'] }
  window.innerWidth = 1400
  show('/w/DEMO/ticket/DEMO-0001')
  expect(await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  await screen.findByRole('complementary', { name: 'Ticket details' })
  for (const k of ['DEMO-0099', 'DEMO-0098', 'DEMO-0097']) {
    expect(screen.getAllByText(k).length).toBeGreaterThan(0)
    expect(screen.queryByRole('link', { name: k })).not.toBeInTheDocument()
  }
  expect(vi.mocked(fetch).mock.calls.some((c) => String(c[0]).includes('DEMO-009'))).toBe(false)
  act(() => queryClient.setQueryData(['tickets', ws, 'all'], [{ uid: 'u9', key: 'DEMO-0099', title: 'Visible parent' }]))
  expect(await screen.findByRole('link', { name: 'DEMO-0099' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'DEMO-0098' })).not.toBeInTheDocument()
})

it('after a revoke the notice says the workspace access is no longer available', async () => {
  vi.mocked(useLiveUpdates).mockReturnValue({ ready: false, revoked: true })
  try {
    show('/w/DEMO/board')
    expect(await screen.findByText('Workspace access is no longer available.')).toBeInTheDocument()
  } finally {
    vi.mocked(useLiveUpdates).mockReturnValue({ ready: true, revoked: false })
  }
})

it('a gone for another ticket leaves the open ticket page in place', async () => {
  show('/w/DEMO/ticket/DEMO-0001')
  expect(await screen.findByText('Real P1 ticket')).toBeInTheDocument()
  await waitFor(() => expect(vi.mocked(useLiveUpdates).mock.calls.at(-1)?.[0]).toBe(ws))
  const close = vi.mocked(useLiveUpdates).mock.calls.at(-1)![1]!
  // The test before closes this same ticket with a toast; a dismissed toast takes a moment to leave the page.
  await waitFor(() => expect(screen.queryByText(/no longer visible to you/)).not.toBeInTheDocument())
  act(() => invalidateLive(queryClient, ws, 'items', [{ id: '9.g', uid: 'ULID-OTHER', gone: true }], close))
  await new Promise((r) => setTimeout(r, 50))
  expect(location.pathname).toBe('/w/DEMO/ticket/DEMO-0001')
  expect(screen.queryByText(/no longer visible to you/)).not.toBeInTheDocument()
})
