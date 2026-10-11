import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from './prodApi'
import { ApiError } from './types'
import board from '@/test/fixtures/server-m2/board-page.json'
import ticket from '@/test/fixtures/server-m2/ticket.json'

vi.mock('./bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn() }))

const seen: string[] = []
const answer = (routes: Record<string, unknown>) =>
  vi.fn(async (url: string) => {
    seen.push(url)
    const hit = Object.entries(routes).find(([prefix]) => url.startsWith(prefix))
    return hit ? Response.json(hit[1]) : Response.json({ ok: false, error: { code: 'not_found', message: 'x', retryable: false } }, { status: 404 })
  })
beforeEach(() => { seen.length = 0 })
afterEach(() => vi.unstubAllGlobals())

const ws = 'a'.repeat(32)

it('reads every page of a list from one snapshot and applies what the host cannot filter', async () => {
  const page2 = { ...board, rows: [{ ...board.rows[1], key: 'DEMO-0003', priority: 'low', uid: 'UID3' }], next_page: null }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => Response.json(url.includes('page=PAGE.2') ? page2 : board)))
  const all = await api.listBoard(ws)
  expect(all.map((t) => t.key)).toEqual(['DEMO-0001', 'DEMO-0002', 'DEMO-0003'])
  expect(vi.mocked(fetch).mock.calls.map((c) => c[0])).toEqual([
    `/api/w/${ws}/board?limit=200`,
    `/api/w/${ws}/board?limit=200&page=PAGE.2`,
  ])
  const high = await api.listTickets(ws, { priority: ['high'], status: ['open', 'waiting'], label: 'api', sort: 'key', needs: 'me' })
  expect(high.map((t) => t.key)).toEqual(['DEMO-0001'])
  const url = String(vi.mocked(fetch).mock.calls.at(-2)![0])
  expect(url).toContain('status=open')
  expect(url).not.toContain('waiting')
  expect(url).toContain('label=api')
  expect(url).toContain('sort=key&order=asc')
  expect(url).toContain('needs_me=true')
  expect(url).not.toContain('priority')
})

it('a status the host does not have matches nothing without asking it', async () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  expect(await api.listTickets(ws, { status: 'waiting' })).toEqual([])
  expect(fetcher).not.toHaveBeenCalled()
})

it('reads a ticket, its log and its changes in the ticket\'s workspace by UUID', async () => {
  vi.stubGlobal('fetch', answer({ [`/api/w/${ws}/tickets/DEMO-0001/events`]: [], [`/api/w/${ws}/tickets/DEMO-0001/changes`]: { files: [] }, [`/api/w/${ws}/tickets/DEMO-0001`]: ticket }))
  expect((await api.getTicket('DEMO-0001', ws)).key).toBe('DEMO-0001')
  await api.getEvents('DEMO-0001', 3, ws)
  await api.getChanges('DEMO-0001', ws)
  expect(seen).toEqual([`/api/w/${ws}/tickets/DEMO-0001`, `/api/w/${ws}/tickets/DEMO-0001/events?since=3`, `/api/w/${ws}/tickets/DEMO-0001/changes`])
  await expect(api.getTicket('DEMO-0001')).rejects.toMatchObject({ code: 'op.unsupported', status: 501 })
})

it('keeps the host\'s error code, status and hint', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ok: false, exit: 3, error: { code: 'observe.unavailable', message: 'moved', hint: 'Open it again', retryable: false } }, { status: 409 })))
  const error = await api.getChanges('DEMO-0001', ws).catch((e: unknown) => e)
  expect(error).toBeInstanceOf(ApiError)
  expect(error).toMatchObject({ status: 409, code: 'observe.unavailable', hint: 'Open it again', exit: 3 })
})

it('answers neutrally where a page needs an answer and refuses every action', async () => {
  vi.stubGlobal('fetch', vi.fn())
  expect(await api.getAddons()).toEqual([])
  expect(await api.getAgents(ws)).toEqual([])
  expect(await api.getAddonDecisions(ws)).toEqual([])
  expect(await api.listGrants(ws)).toEqual([])
  expect(await api.getDataset()).toEqual({ dataset: 'normal' })
  for (const call of [() => api.postAction('DEMO-0001', { action: 'comment', text: 'x' } as never), () => api.createTicket(ws, {} as never), () => api.setViewer('p_x'), () => api.runAddonAction(ws, 'a', 'b')]) {
    await expect(call()).rejects.toMatchObject({ code: 'op.unsupported' })
  }
  expect(fetch).not.toHaveBeenCalled()
})

it('takes the highest role across workspaces for the person', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([
    { person: 'p_1', name: 'Sev', role: 'member', tier: 'A', workspace: 'w1' },
    { person: 'p_1', name: 'Sev', role: 'owner', tier: 'A', workspace: 'w2' },
  ])))
  expect(await api.getMe()).toEqual({ person: 'p_1', name: 'Sev', role: 'owner' })
})
