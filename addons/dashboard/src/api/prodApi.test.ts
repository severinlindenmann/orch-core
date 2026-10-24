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

it('reads the viewer as each workspace knows them (a different person id per workspace)', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([
    { person: 'p_1', name: 'Sev', role: 'member', tier: 'A', workspace: 'w1' },
    { person: 'p_2', name: 'Severin', role: 'owner', tier: 'A', workspace: 'w2' },
  ])))
  expect(await api.getMe('w1')).toEqual({ person: 'p_1', name: 'Sev', role: 'member' })
  expect(await api.getMe('w2')).toEqual({ person: 'p_2', name: 'Severin', role: 'owner' })
  await expect(api.getMe('w3')).rejects.toMatchObject({ status: 404 })
})

it('speaks the four click routes with POST bodies and keeps reason and wait on a refusal', async () => {
  const answers = vi.fn(async (_url: string, init?: RequestInit) => {
    if (String(_url).endsWith('/click/sign')) return Response.json({ ok: false, error: { code: 'rate.limited', message: 'slow', retryable: true, retry_after_ms: 4200, reason: 'x' } }, { status: 503 })
    return Response.json({ request_id: 'hr_x', init: String(init?.body) })
  })
  vi.stubGlobal('fetch', answers)
  const action = { action: 'approve', key: 'DEMO-0001', gate: 'plan' } as const
  await api.prepareClick(ws, action)
  await api.prepareClick(ws, action, 2)
  await api.viewClick(ws, 'hr_1')
  await api.cancelClick(ws, 'hr_1')
  const calls = answers.mock.calls.map((c) => [String(c[0]), (c[1] as RequestInit).method, JSON.parse(String((c[1] as RequestInit).body))])
  expect(calls).toEqual([
    [`/api/w/${ws}/click/prepare`, 'POST', { action: 'approve', key: 'DEMO-0001', gate: 'plan' }],
    [`/api/w/${ws}/click/prepare`, 'POST', { action: 'approve', key: 'DEMO-0001', gate: 'plan', retry: 2 }],
    [`/api/w/${ws}/click/view`, 'POST', { request_id: 'hr_1' }],
    [`/api/w/${ws}/click/cancel`, 'POST', { request_id: 'hr_1' }],
  ])
  await expect(api.signClick(ws, 'hr_1', 'sha256:' + 'a'.repeat(64))).rejects.toMatchObject({ code: 'rate.limited', retry_after_ms: 4200, reason: 'x' })
  expect(JSON.parse(String((answers.mock.calls[4][1] as RequestInit).body))).toEqual({ request_id: 'hr_1', view_digest: 'sha256:' + 'a'.repeat(64) })
})

it('offers a workspace the click card only when the server lists the click feature', async () => {
  const row = (features: string[]) => ({ id: ws, prefix: 'DEMO', name: 'b', members: [], needs_you: 0, host_state: 'ok', features, click: { custody: 'convenience', enabled: true, available: true } })
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([row(['actions', 'click'])])))
  expect((await api.getWorkspaces())[0].click).toEqual({ custody: 'convenience', enabled: true, available: true })
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([row(['actions'])])))
  expect((await api.getWorkspaces())[0].click).toEqual({ custody: 'convenience', enabled: true, available: false })
})

it('a workspace list read refreshes the features: a newly enabled add-on shows in the menu without a reload (run 8, 1.7)', async () => {
  const { setFeatures, hasFeature } = await import('./features')
  const row = (features: string[]) => ({ id: ws, prefix: 'DEMO', name: 'b', members: [], needs_you: 0, host_state: 'ok', features })
  setFeatures({ features: ['actions'], workspaces: { [ws]: ['actions'] } })
  expect(hasFeature('usage', ws)).toBe(false)
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([row(['actions', 'usage'])])))
  await api.getWorkspaces()
  expect(hasFeature('usage', ws)).toBe(true)
  expect(hasFeature('usage')).toBe(true)
  setFeatures(null)
})

it('asks the server where a click request stands (the answer to an unknown outcome)', async () => {
  const answers = vi.fn(async (_url: string) => Response.json({ id: 'hr_1', state: 'bound' }))
  vi.stubGlobal('fetch', answers)
  expect(await api.stateClick(ws, 'hr_1')).toEqual({ id: 'hr_1', state: 'bound' })
  expect(String(answers.mock.calls[0][0])).toBe(`/api/w/${ws}/click/state`)
})
