import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { expect, it } from 'vitest'

// Opus 7.1: file content is only ever drawn as React text from shiki's token API, never as an HTML string.
function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f)
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f) ? [p] : []
  })
}
it('no source file uses codeToHtml or shiki HTML output', () => {
  const root = join(__dirname, '..')
  for (const f of files(root)) expect(readFileSync(f, 'utf8'), f).not.toMatch(/codeToHtml|codeToHast/)
})
