import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'

const setup = (viewer?: string) => {
  const store = createMockStore({ persist: false })
  if (viewer) store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type Page = { slug: string; title: string; markdown: string; by: string; tickets: string[] }
type Item = { title: string; subtitle?: string; actions?: { action: string; args?: Record<string, unknown> }[] }
type State = {
  pages: Page[]
  current: { slug: string; title: string; markdown: string }
  editForm: { title: string; markdown: string }
  items: Item[]
  slugs: string[]
  byTicket: Record<string, Item[]>
}
const state = async (s: ReturnType<typeof setup>) => (await s.api.getAddonState(s.ws, 'wiki')) as unknown as State

describe('wiki seed', () => {
  it('has 7 pages, one with a code block and a table and one with hostile markup', async () => {
    const st = await state(setup())
    expect(st.pages).toHaveLength(7)
    expect(new Set(st.pages.map((p) => p.slug)).size).toBe(7)
    expect(st.pages.some((p) => p.markdown.includes('```') && p.markdown.includes('| --- |'))).toBe(true)
    const hostile = st.pages.find((p) => p.title === 'Imported from old wiki')!
    expect(hostile.markdown).toContain('<script>')
    expect(hostile.markdown).toContain('javascript:')
  })
  it('lists every page with an open action carrying its slug', async () => {
    const st = await state(setup())
    expect(st.items).toHaveLength(7)
    expect(st.items[0].actions![0]).toMatchObject({ action: 'open', args: { slug: st.pages[0].slug } })
    expect(st.current.slug).toBe(st.pages[0].slug)
  })
})

describe('wiki actions', () => {
  it('open sets the current page and the edit form follows', async () => {
    const s = setup()
    const target = (await state(s)).pages[3]
    await s.api.runAddonAction(s.ws, 'wiki', 'open', { slug: target.slug })
    const st = await state(s)
    expect(st.current.slug).toBe(target.slug)
    expect(st.editForm).toEqual({ title: target.title, markdown: target.markdown })
  })
  it('open with an unknown slug changes nothing', async () => {
    const s = setup()
    const before = (await state(s)).current.slug
    await s.api.runAddonAction(s.ws, 'wiki', 'open', { slug: 'nope' })
    expect((await state(s)).current.slug).toBe(before)
  })
  it('save updates the current page, its author and its date', async () => {
    const s = setup('p_mara')
    await s.api.runAddonAction(s.ws, 'wiki', 'save', { formData: { title: 'New title', markdown: '# Fresh text' } })
    const st = await state(s)
    expect(st.current).toMatchObject({ title: 'New title', markdown: '# Fresh text' })
    const p = st.pages.find((x) => x.slug === st.current.slug) as Page & { updated: string }
    expect(p.by).toBe('Mara')
    expect(p.updated.startsWith('2026-10-09')).toBe(true)
  })
  it('search filters the list by title and text and an empty query restores it', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'wiki', 'search', { formData: { query: 'runbook' } })
    const hit = await state(s)
    expect(hit.items.length).toBeGreaterThan(0)
    expect(hit.items.length).toBeLessThan(7)
    expect(hit.items.every((i) => /runbook/i.test(i.title))).toBe(true)
    await s.api.runAddonAction(s.ws, 'wiki', 'search', { formData: { query: '' } })
    expect((await state(s)).items).toHaveLength(7)
  })
  it('link attaches a page to the ticket in the body; the panel map reads it', async () => {
    const s = setup()
    const st0 = await state(s)
    expect(st0.byTicket['DEMO-0043'].length).toBeGreaterThanOrEqual(2)
    const free = st0.pages.find((p) => !p.tickets.includes('DEMO-0042'))!
    await s.api.runAddonAction(s.ws, 'wiki', 'link', { ticket: 'DEMO-0042', formData: { page: free.title } })
    const st = await state(s)
    expect(st.byTicket['DEMO-0042'].map((i) => i.title)).toContain(free.title)
  })
  it('viewers cannot edit, search-state aside: save, link and open are refused', async () => {
    const s = setup('p_tom')
    for (const id of ['save', 'link', 'open']) await expect(s.api.runAddonAction(s.ws, 'wiki', id, { slug: 'x' })).rejects.toMatchObject({ status: 403 })
  })
})
