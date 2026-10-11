import { describe, expect, it, vi } from 'vitest'
import { createApi } from './client'
import { createFetchTransport, createMockTransport } from './transport'
import { createMockStore } from '@/mocks/store'

describe('preview transport boundary', () => {
  it('off reads stay local; on reads use the preview endpoint', async () => {
    const store = createMockStore({ persist: false })
    const ws = store.workspaces[0].id
    const transport = createMockTransport(store, { latency: false })
    const request = vi.spyOn(transport, 'request')
    const api = createApi(transport)
    expect((await api.getMandatesPreview(ws)).on).toBe(false)
    expect(request).not.toHaveBeenCalled()
    await api.postMandatesPreview(ws, { op: 'enable' })
    request.mockClear()
    expect((await api.getMandatesPreview(ws)).on).toBe(true)
    expect(request).toHaveBeenCalledWith('GET', `/api/workspaces/${ws}/preview/mandates`, undefined)
  })
  it('never sends preview reads or writes to a real host', async () => {
    const transport = createFetchTransport('https://host.example.test')
    const request = vi.spyOn(transport, 'request').mockResolvedValue({ status: 200, json: {} })
    const api = createApi(transport)
    await expect(api.getMandatesPreview('ws')).rejects.toThrow(/preview/i)
    await expect(api.postMandatesPreview('ws', { op: 'enable' })).rejects.toThrow(/preview/i)
    expect(request).not.toHaveBeenCalled()
  })
})
