import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'

describe('ticket create type check', () => {
  it('rejects inherited keys such as toString as a ticket type', async () => {
    const store = createMockStore({ persist: false })
    const api = createApi(createMockTransport(store, { latency: false }))
    const ws = store.workspaces[0].id
    for (const type of ['toString', 'constructor', '__proto__'])
      await expect(api.createTicket(ws, { type, title: 'Valid title' } as never)).rejects.toMatchObject({ status: 400, code: 'validation', message: 'Body must be a new ticket.' })
  })
})
