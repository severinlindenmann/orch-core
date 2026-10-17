import { describe, expect, it } from 'vitest'
import { SetupAPI } from './setup'

describe('SetupAPI', () => {
  it('calls its fetcher unbound, like the browser requires of window.fetch', async () => {
    // A browser's fetch throws "Illegal invocation" when `this` is anything but the window (or undefined).
    function strictFetch(this: unknown, ..._args: Parameters<typeof fetch>): Promise<Response> {
      if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation')
      return Promise.resolve(new Response(JSON.stringify({ phase: 'profile' }), { status: 200 }))
    }
    const api = new SetupAPI('demo-not-a-real-token', strictFetch as typeof fetch)
    await expect(api.call('state')).resolves.toEqual({ phase: 'profile' })
  })
})
