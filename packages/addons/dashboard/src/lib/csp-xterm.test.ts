import { readFileSync } from 'node:fs'
import path from 'node:path'
import { expect, it } from 'vitest'
import config from '../../vite.config'

// The real xterm file, run through the csp-no-inline-style plugin: nothing may be left that the CSP (style-src 'self') blocks.
it('csp-no-inline-style leaves no style attribute or <style> element in xterm', async () => {
  const plugins = ((await (config as unknown as (e: { command: string }) => unknown)({ command: 'build' })) as { plugins: { name?: string; transform?: (c: string, id: string) => { code: string } | null }[] }).plugins
  const plugin = plugins.find((p) => p?.name === 'csp-no-inline-style')!
  const id = path.resolve(import.meta.dirname, '../../node_modules/@xterm/xterm/lib/xterm.js')
  const out = plugin.transform!.call({}, readFileSync(id, 'utf8'), id)!.code
  expect(out).not.toContain('setAttribute("style"')
  expect(out).not.toContain('createElement("style")')
  expect(out).toContain('e.style.cssText=')
})
