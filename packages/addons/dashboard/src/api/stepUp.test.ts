import { afterEach, expect, it, vi } from 'vitest'
import { createFetchTransport } from './fetchTransport'
import { onStepUp, settleStepUp, stepUpOf } from './stepUp'

const refusal = { ok: false, error: { code: 'step_up_required', message: 'Confirm.', retryable: false, step_up: { where: 'this_computer' } } }
const off: (() => void)[] = []
afterEach(() => { vi.unstubAllGlobals(); off.splice(0).forEach((f) => f()); settleStepUp(false) })

it('recognises the structured refusal and nothing else', () => {
  expect(stepUpOf(403, refusal)).toEqual({ where: 'this_computer', message: 'Confirm.' })
  expect(stepUpOf(403, { error: { code: 'remote.refused' } })).toBeNull()
  expect(stepUpOf(500, refusal)).toBeNull()
  expect(stepUpOf(403, null)).toBeNull()
})

it('a refused change asks once, then runs the same action again after the step-up', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json(refusal, { status: 403 }))
    .mockResolvedValueOnce(Response.json({ ok: true }, { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  const asked = vi.fn()
  off.push(onStepUp((i) => { if (i) { asked(i); settleStepUp(true) } }))
  const res = await createFetchTransport('', () => 'old').request('POST', '/api/w/x/tickets', { a: 1 })
  expect(asked).toHaveBeenCalledTimes(1)
  expect(res).toEqual({ status: 200, json: { ok: true } })
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('declining the step-up returns the refusal and does not repeat the action', async () => {
  const fetcher = vi.fn(async () => Response.json(refusal, { status: 403 }))
  vi.stubGlobal('fetch', fetcher)
  off.push(onStepUp((i) => { if (i) settleStepUp(false) }))
  const res = await createFetchTransport('', () => 'old').request('DELETE', '/api/w/x/terminals/t')
  expect(res.status).toBe(403)
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('a refused read never opens the question', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json(refusal, { status: 403 })))
  const asked = vi.fn()
  off.push(onStepUp(asked))
  await createFetchTransport('', () => 'old').request('GET', '/api/x')
  expect(asked).not.toHaveBeenCalledWith(expect.objectContaining({ where: expect.anything() }))
})
