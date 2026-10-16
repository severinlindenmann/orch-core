import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { REAL_HOST_PAGES, selectsRealHost } from '../../scripts/layout-guard-pages.mjs'
import { LAYOUT_SCENARIOS } from './layoutPreview'

const guard = readFileSync(resolve(process.cwd(), 'scripts/layout-guard.mjs'), 'utf8')

it('--only pair selects the pairing pages (the real-host block is entered)', () => {
  expect(selectsRealHost(/pair/)).toBe(true)
  expect(REAL_HOST_PAGES.filter((n) => /pair/.test(n)).length).toBeGreaterThanOrEqual(7)
  expect(selectsRealHost(/terminal/)).toBe(true)
  expect(selectsRealHost(/^board$/)).toBe(false)
  expect(guard).toContain('if (selectsRealHost(ONLY)) {')
})

it('every pair layout scenario has a real-host-pair-* guard page, listed and checked', () => {
  for (const s of LAYOUT_SCENARIOS.filter((x) => x.startsWith('pair-'))) {
    expect(REAL_HOST_PAGES).toContain(`real-host-${s}`)
    expect(guard).toContain(`['real-host-${s}', '${s}',`)
  }
  for (const n of REAL_HOST_PAGES) expect(guard, `${n} is named in the guard`).toContain(`'${n}'`)
})

it('every drop layout scenario has a real-host-drop-* guard page, listed and checked', () => {
  expect(selectsRealHost(/drop/)).toBe(true)
  for (const s of LAYOUT_SCENARIOS.filter((x) => x.startsWith('drop-'))) {
    expect(REAL_HOST_PAGES).toContain(`real-host-${s}`)
    expect(guard).toContain(`['real-host-${s}', '${s}',`)
  }
})

it('the H12d-2 reader scenarios have guard pages, listed and checked', () => {
  for (const s of ['readers-devices', 'convenience-refusals'] as const) {
    expect(LAYOUT_SCENARIOS).toContain(s)
    expect(REAL_HOST_PAGES).toContain(`real-host-${s}`)
    expect(guard).toContain(`['real-host-${s}', '${s}',`)
  }
  expect(selectsRealHost(/readers|refusals/)).toBe(true)
})

it('the core D1 dev-host scenarios have guard pages, listed and checked', () => {
  for (const s of ['dev-badge', 'dev-badge-long', 'dev-alarms', 'dev-refused', 'signing-dev-host'] as const) {
    expect(LAYOUT_SCENARIOS).toContain(s)
    expect(REAL_HOST_PAGES).toContain(`real-host-${s}`)
    expect(guard).toContain(`['real-host-${s}', '${s}',`)
  }
  expect(selectsRealHost(/dev-/)).toBe(true)
})
