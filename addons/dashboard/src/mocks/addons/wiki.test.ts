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
  current: { slug: string; title: string; markdown: string; meta: string }
  editForm: { slug: string; title: string; markdown: string }
  items: Item[]
  pageOptions: { const: string; title: string }[]
  byTicket: Record<string, Item[]>
}
type S = ReturnType<typeof setup>
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'wiki')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'wiki', id, body)
/** The same store seen as another person. */
const as = (s: S, person: string) => {
  s.store.setViewer(person)
  return s
}

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
  it('lists every page with an open action carrying its slug; the first page is open by default', async () => {
    const st = await state(setup())
    expect(st.items).toHaveLength(7)
    expect(st.items[0].actions![0]).toMatchObject({ action: 'open', args: { slug: st.pages[0].slug } })
    expect(st.current.slug).toBe(st.pages[0].slug)
    expect(st.current.meta).toMatch(/^by Mara · updated \d+d ago$/)
  })
})

describe('wiki actions', () => {
  it('open sets the current page and the edit form (with its slug) follows', async () => {
    const s = setup()
    const target = (await state(s)).pages[3]
    await run(s, 'open', { slug: target.slug })
    const st = await state(s)
    expect(st.current.slug).toBe(target.slug)
    expect(st.editForm).toEqual({ slug: target.slug, title: target.title, markdown: target.markdown })
  })
  it('open by title or with an unknown slug changes nothing: pages are addressed by slug only', async () => {
    const s = setup()
    const st0 = await state(s)
    await run(s, 'open', { slug: 'nope' })
    await run(s, 'open', { slug: st0.pages[2].title })
    expect((await state(s)).current.slug).toBe(st0.current.slug)
  })
  it('save writes to the page named in the form, even after the open page changed meanwhile', async () => {
    const s = setup()
    const [a, b] = (await state(s)).pages
    await run(s, 'open', { slug: b.slug })
    const form = (await state(s)).editForm // the draft the user is typing into
    await run(s, 'open', { slug: a.slug }) // same person navigates elsewhere before pressing save
    await run(s, 'save', { formData: { ...form, markdown: '# Edited B' } })
    const st = await state(s)
    expect(st.pages.find((p) => p.slug === a.slug)!.markdown).toBe(a.markdown)
    expect(st.pages.find((p) => p.slug === b.slug)!.markdown).toBe('# Edited B')
  })
  it('save records the author and date, and refuses an empty or duplicate title', async () => {
    const s = as(setup(), 'p_mara')
    const [a, b] = (await state(s)).pages
    await run(s, 'save', { formData: { slug: a.slug, title: 'New title', markdown: '# Fresh text' } })
    let st = await state(s)
    const p = st.pages.find((x) => x.slug === a.slug) as Page & { updated: string }
    expect(p).toMatchObject({ title: 'New title', markdown: '# Fresh text', by: 'Mara' })
    expect(p.updated.startsWith('2026-10-09')).toBe(true)
    await run(s, 'save', { formData: { slug: a.slug, title: b.title.toUpperCase(), markdown: 'dup' } })
    await run(s, 'save', { formData: { slug: a.slug, title: '  ', markdown: 'empty' } })
    st = await state(s)
    expect(st.pages.find((x) => x.slug === a.slug)).toMatchObject({ title: 'New title', markdown: '# Fresh text' })
  })
  it('search filters the list by title and text and an empty query restores it', async () => {
    const s = setup()
    await run(s, 'search', { formData: { query: 'runbook' } })
    const hit = await state(s)
    expect(hit.items.length).toBeGreaterThan(0)
    expect(hit.items.length).toBeLessThan(7)
    expect(hit.items.every((i) => /runbook/i.test(i.title))).toBe(true)
    await run(s, 'search', { formData: { query: '' } })
    expect((await state(s)).items).toHaveLength(7)
  })
  it('link attaches a page, by slug, to the ticket in the body; the panel map reads it', async () => {
    const s = setup()
    const st0 = await state(s)
    expect(st0.byTicket['DEMO-0043'].length).toBeGreaterThanOrEqual(2)
    expect(st0.pageOptions[0]).toEqual({ const: st0.pages[0].slug, title: st0.pages[0].title })
    const free = st0.pages.find((p) => !p.tickets.includes('DEMO-0042'))!
    await run(s, 'link', { ticket: 'DEMO-0042', formData: { page: free.title } }) // a title is not an address
    expect((await state(s)).byTicket['DEMO-0042'].map((i) => i.title)).not.toContain(free.title)
    await run(s, 'link', { ticket: 'DEMO-0042', formData: { page: free.slug } })
    expect((await state(s)).byTicket['DEMO-0042'].map((i) => i.title)).toContain(free.title)
  })
})

describe('wiki navigation is per viewer', () => {
  it("one member opening or searching does not change another member's view", async () => {
    const s = setup('p_sev')
    const pages = (await state(s)).pages
    await run(s, 'open', { slug: pages[4].slug })
    await run(s, 'search', { formData: { query: 'glossary' } })
    as(s, 'p_mara')
    const mara = await state(s)
    expect(mara.current.slug).toBe(pages[0].slug)
    expect(mara.items).toHaveLength(7)
    as(s, 'p_sev')
    const sev = await state(s)
    expect(sev.current.slug).toBe(pages[4].slug)
    expect(sev.items.length).toBeLessThan(7)
  })
  it('a viewer can open and search (changing only their own view) but cannot save or link', async () => {
    const s = setup('p_tom')
    const pages = (await state(s)).pages
    await run(s, 'open', { slug: pages[2].slug })
    await run(s, 'search', { formData: { query: 'tolerance' } })
    const tom = await state(s)
    expect(tom.current.slug).toBe(pages[2].slug)
    expect(tom.items.length).toBeLessThan(7)
    as(s, 'p_sev')
    expect((await state(s)).current.slug).toBe(pages[0].slug)
    as(s, 'p_tom')
    const body = { ticket: 'DEMO-0042', formData: { slug: pages[0].slug, title: 'Hacked', markdown: 'x', page: pages[0].slug } }
    await expect(run(s, 'save', body)).rejects.toMatchObject({ status: 403 })
    await expect(run(s, 'link', body)).rejects.toMatchObject({ status: 403 })
    expect((await state(s)).pages[0].title).toBe(pages[0].title)
  })
})
