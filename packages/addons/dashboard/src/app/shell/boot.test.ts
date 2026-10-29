import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, it } from 'vitest'

// vitest runs from the package root (packages/addons/dashboard)
const read = (p: string) => readFileSync(join(process.cwd(), p), 'utf8')

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

it('the root route keeps the boot screen while the shell data loads (never a null pending component)', () => {
  const router = read('src/app/router.tsx')
  expect(router).toMatch(/pendingComponent: BootScreen,\s*pendingMinMs: 0/)
  expect(read('src/app/shell/BootScreen.tsx')).toMatch(/className="boot"[\s\S]*Loading…/)
  expect(read('src/production/Shell.tsx')).not.toContain('prod-skeleton')
})
