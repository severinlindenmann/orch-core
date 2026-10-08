import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { addonActive } from '@/api/addons'
import { TEMPLATES, templateDigest } from '@/api/widgetTemplates'
import { sha256Hex } from '@/api/sha256'
import { createMockStore } from '@/mocks/store'

const setup = () => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}

describe('widgets addon (starts installed)', () => {
  it('is installed, granted and active in DEMO', () => {
    const { store, ws } = setup()
    const w = store.workspaces.find((x) => x.id === ws)!
    expect(addonActive(w, 'widgets')).toBe(true)
    expect(store.addons.find((a) => a.name === 'widgets')).toMatchObject({ first_party: true, capabilities: [] })
  })
  it('state lists the templates with their pin and the core types it draws', async () => {
    const { api, ws } = setup()
    const st = (await api.getAddonState(ws, 'widgets')) as unknown as { templates: { name: string; version: number; digest: string }[]; coreTypes: { type: string }[] }
    expect(st.templates.map((t) => `${t.name}@${t.version}`)).toEqual(expect.arrayContaining(['before-after@1', 'line-chart@1', 'option-prototype@1']))
    for (const t of st.templates) expect(t.digest).toMatch(/^[0-9a-f]{64}$/)
    expect(st.coreTypes.map((c) => c.type)).toEqual(['bars', 'table', 'checks', 'kv'])
  })
})

const subtle = async (s: string) =>
  [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)))].map((b) => b.toString(16).padStart(2, '0')).join('')

describe('template registry', () => {
  it('pins are sha256 over the page, a NUL byte and the sorted libs as compact JSON', async () => {
    for (const t of TEMPLATES) expect(templateDigest(t)).toBe(await subtle(t.html + '\0' + JSON.stringify([...t.libs].sort())))
  })
  it('template pages have no network or navigation hooks and use no innerHTML', () => {
    for (const t of TEMPLATES) {
      expect(t.html).not.toMatch(/\bfetch\s*\(|XMLHttpRequest|WebSocket|location\s*[.=]|window\.open|<a\s|<form|innerHTML|document\.write|src\s*=\s*["']?https?:/i)
    }
  })
})

describe('seeded widget fixtures pin what they show', () => {
  const ws = () => setup()
  it('every html widget pin matches the sha256 of the artifact content, except the one seeded to mismatch', async () => {
    const { store } = ws()
    const t = store.ticket('DEMO-0041')!
    const good = t.artifacts.find((a) => a.name === 'reconciliation-demo.html')!
    const other = t.artifacts.find((a) => a.name === 'tolerance-demo.html')!
    expect(good.sha256).toBe(await subtle(good.preview!))
    expect(sha256Hex(other.preview!)).toBe(other.sha256)
    expect(other.sha256).toBe(await subtle(other.preview!))
    expect(t.body.verification).toContain(good.sha256)
    expect(t.body.verification).not.toContain(other.sha256) // pinned to an older version of the page
  })
  it('every template widget in the seed pins the current digest, except the drift case', () => {
    const { store } = ws()
    const pins = new Set(TEMPLATES.map(templateDigest))
    const all = ['DEMO-0043', 'DEMO-0046'].flatMap((k) => Object.values(store.ticket(k)!.body).flatMap((t) => [...(t ?? '').matchAll(/"widget"\s*:\s*"([^"@]+)@\d+"\s*,\s*"sha256"\s*:\s*"([0-9a-f]{64})"/g)].filter((m) => TEMPLATES.some((t) => t.name === m[1])).map((m) => m[2])))
    expect(all.length).toBeGreaterThanOrEqual(4)
    expect(all.filter((p) => pins.has(p)).length).toBeGreaterThanOrEqual(3)
    expect(all.filter((p) => !pins.has(p)).length).toBe(1)
  })
})
