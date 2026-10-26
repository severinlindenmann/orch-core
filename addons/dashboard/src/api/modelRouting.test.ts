import { afterEach, expect, it, vi } from 'vitest'
import * as features from './features'
import * as runs from './runs'
import { routingApi } from './modelRouting'

afterEach(() => vi.restoreAllMocks())

// Regression: the focus "Start an agent" dialog crashed in the mock demo (reading `find` of undefined) because
// `/model-routing` was answered with the dev server's HTML page, a success without `harnesses`.
it('the mock host answers with a closed list of harnesses and never calls the network', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(false)
  const call = vi.spyOn(runs, 'call')
  const got = await routingApi.choices('DEMO')
  expect(got.harnesses.length).toBeGreaterThan(0)
  expect(got.harnesses.every((h) => Array.isArray(h.models))).toBe(true)
  expect(call).not.toHaveBeenCalled()
})

it('a real host reply without harnesses is an error, not data', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  vi.spyOn(runs, 'call').mockResolvedValue({} as never)
  await expect(routingApi.choices('DEMO')).rejects.toThrow('unexpected reply')
})
