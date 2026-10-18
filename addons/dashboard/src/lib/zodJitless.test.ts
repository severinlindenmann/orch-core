// No eval path may run in the browser: the dashboard's CSP has no 'unsafe-eval' (zod's JIT probe would be a violation).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'

describe('zod without eval', () => {
  it('builds and runs an object schema without ever calling the Function constructor', async () => {
    vi.resetModules()
    const calls: unknown[][] = []
    const real = globalThis.Function
    globalThis.Function = new Proxy(real, { construct(target, args) { calls.push(args); return Reflect.construct(target, args) }, apply(target, self, args) { calls.push(args); return Reflect.apply(target, self, args) } }) as FunctionConstructor
    try {
      await import('./zodJitless')
      const { z } = await import('zod')
      const schema = z.object({ a: z.string(), b: z.array(z.object({ c: z.number().optional() })) })
      expect(schema.parse({ a: 'x', b: [{ c: 1 }, {}] })).toEqual({ a: 'x', b: [{ c: 1 }, {}] })
      expect(calls).toEqual([])
    } finally {
      globalThis.Function = real
    }
  })
  it('is the first import of every entry file (before any schema is built)', () => {
    for (const entry of ['../main.tsx', '../main.prod.tsx']) {
      const first = readFileSync(resolve(__dirname, entry), 'utf8').split('\n').find((l) => l.startsWith('import '))
      expect(first, entry).toBe("import './lib/zodJitless'")
    }
  })
})
