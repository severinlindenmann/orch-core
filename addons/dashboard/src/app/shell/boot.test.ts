import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'

const root = new URL('../../../', import.meta.url)
const read = (p: string) => readFileSync(new URL(p, root), 'utf8')

it('index.html paints a loading shell before any script, without inline styles (the CSP blocks them)', () => {
  const html = read('index.html')
  expect(html).toMatch(/<div id="root">\s*<div class="boot"[^>]*>[\s\S]*Loading…[\s\S]*<\/div>\s*<\/div>/)
  expect(html).toContain('href="/loading.css"')
  expect(html).not.toMatch(/<style[\s>]/)
  expect(html).not.toMatch(/\sstyle="/)
  expect(read('public/loading.css')).toContain('.boot')
})

it('nothing in the shell forces a 1024 px page width, so a phone does not scroll sideways', () => {
  expect(read('src/app/shell/Shell.tsx')).not.toMatch(/min-w-\[1024px\]/)
  expect(read('src/styles/tokens.css')).not.toMatch(/min-width:\s*1024px/)
})
