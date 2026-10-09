import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { refused } from '@/test/refused'

const setup = (viewer?: string) => {
  const store = createMockStore({ persist: false })
  if (viewer) store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type Page = { slug: string; title: string; markdown: string; by: string; tickets: string[] }
type Item = { title: string; subtitle?: string; actions?: { action: string; args?: Record<string, unknown> }[] }
type Node = { type: string; [k: string]: unknown }
type Tab = { id: string; label: string; count?: number; node: Node }
const tabsOf = (st: { listView: { children?: Node[] } }) => (st.listView.children!.find((c) => c.type === 'tabs') as unknown as { tabs: Tab[] }).tabs
const find = (n: unknown, type: string): Node | undefined => {
  if (!n || typeof n !== 'object') return undefined
  const o = n as Node
  if (o.type === type) return o
  for (const v of Object.values(o)) for (const x of Array.isArray(v) ? v : [v]) { const r = find(x, type); if (r) return r }
  return undefined
}
type State = {
  pages: Page[]
  current: { slug: string; title: string; markdown: string; meta: string; toc: { level: number; text: string }[] } | null
  editing: boolean
  pageRows: { slug: string; page: string; tickets: number }[]
  pageView: { type: string; children: Node[] }
  listView: { type: string; tabs?: Tab[]; children?: Node[] }
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
  it('shows the list first: tabs Pages, Recently updated and Linked to tickets; the title is the way in', async () => {
    const st = await state(setup())
    expect(st.current).toBeNull()
    expect(st.pageView.children).toHaveLength(0)
    const tabs = tabsOf(st)
    expect(tabs.map((t) => t.label)).toEqual(['Pages', 'Recently updated', 'Linked to tickets'])
    expect(tabs[0].count).toBe(7)
    expect(st.pageRows).toHaveLength(7)
    const pages = tabs[0].node as unknown as { rowOpen: { action: string; args: { slug: string } }; rowActions?: unknown }
    expect(pages.rowOpen).toMatchObject({ action: 'open', args: { slug: '$row.slug' } })
    expect(pages.rowActions).toBeUndefined() // no separate Open button
    const linked = tabs[2].node as unknown as { rows: { ticket: string; page: string; title: string }[] }
    expect(linked.rows.some((r) => r.ticket === 'DEMO-0043' && r.page === 'Reconciliation tolerance' && r.title !== '')).toBe(true)
    expect(tabs[2].count).toBe(3) // tickets, not pages
  })
  it('Recently updated lists every page of the last 14 days, newest first, and says how many of all', async () => {
    const st = await state(setup())
    const recent = tabsOf(st)[1]
    const stack = recent.node as unknown as { children: { text?: string; rows?: { page: string }[] }[] }
    expect(stack.children[1].rows![0].page).toBe('Reconciliation tolerance')
    expect(stack.children[1].rows!.map((r) => r.page)).not.toContain('Imported from old wiki') // 30 days old
    expect(stack.children[0].text).toBe(`${recent.count} of 7 pages changed in the last 14 days. Every page is on the Pages tab.`)
  })
  it('a search with no match offers Clear search; members get New page, viewers do not', async () => {
    const s = setup()
    await run(s, 'search', { formData: { query: 'zzzz' } })
    const tab = tabsOf(await state(s))[0].node as unknown as { children: { type: string; label?: string }[] }
    expect(tab.children.map((c) => c.type)).toEqual(['markdown', 'button'])
    expect(tab.children[1].label).toBe('Clear search')
    await run(s, 'clear_search')
    expect((await state(s)).pageRows).toHaveLength(7)
    expect(JSON.stringify((await state(s)).listView)).toContain('New page')
    expect(JSON.stringify((await state(as(s, 'p_tom'))).listView)).not.toContain('New page')
  })
  it('create makes a page, opens it in edit mode, and refuses a duplicate or empty title', async () => {
    const s = setup()
    await run(s, 'create', { formData: { title: 'Release checklist' } })
    const st = await state(s)
    expect(st.current).toMatchObject({ title: 'Release checklist' })
    expect(st.editing).toBe(true)
    expect(st.pages).toHaveLength(8)
    expect(await refused(run(s, 'create', { formData: { title: 'release CHECKLIST' } }))).toMatchObject({ status: 409, code: 'wiki.title_taken' })
    expect(await refused(run(s, 'create', { formData: { title: '  ' } }))).toMatchObject({ status: 400 })
    await expect(run(as(s, 'p_tom'), 'create', { formData: { title: 'Nope' } })).rejects.toMatchObject({ status: 403 })
  })
})

describe('wiki actions', () => {
  it('open shows the page: title first, one meta line, ticket backlinks, and "On this page" from core (toc)', async () => {
    const s = setup()
    const target = (await state(s)).pages[0]
    await run(s, 'open', { slug: target.slug })
    const st = await state(s)
    expect(st.current!.slug).toBe(target.slug)
    expect(st.current!.meta).toMatch(/^by Mara · updated \d+d ago$/)
    expect(st.current!.toc.map((h) => h.text)).toEqual(['Rules', 'Checks', 'Loader query'])
    expect(st.listView.children).toEqual([]) // the list gives way to the page
    const [crumbs, body] = st.pageView.children as unknown as { children: { type: string; label?: string; text?: string; toc?: boolean; rows?: { ticket: string; title: string }[] }[] }[]
    expect(crumbs.children.map((c) => c.label ?? c.text)).toEqual(['All pages', '/ **Tariff data conventions**', 'Edit page'])
    expect(body.children[0].text).toBe('# Tariff data conventions\n\n*by Mara · updated 2d ago*')
    const back = body.children.find((c) => c.type === 'table')!
    expect(back.rows!.map((r) => r.ticket)).toEqual(['DEMO-0041', 'DEMO-0043'])
    expect(back.rows!.every((r) => r.title)).toBe(true)
    const text = body.children.at(-1)!
    expect(text.toc).toBe(true)
    expect(text.text!.startsWith('# ')).toBe(false) // the title is not repeated in the body
  })
  it('edit in place is a per-viewer toggle; the form carries the slug, saving or closing leaves it', async () => {
    const s = setup()
    const target = (await state(s)).pages[3]
    await run(s, 'open', { slug: target.slug })
    expect(await state(s)).toMatchObject({ editing: false })
    await run(s, 'edit')
    let st = await state(s)
    expect(st.editing).toBe(true)
    const form = find(st.pageView, 'form') as unknown as { formData: unknown; submitLabel: string; cancel: { label: string; action: string } }
    expect(form.formData).toEqual({ slug: target.slug, title: target.title, markdown: target.markdown })
    expect(form.submitLabel).toBe('Save changes')
    expect(form.cancel).toEqual({ label: 'Cancel', action: 'done' }) // no "Done editing"
    expect(JSON.stringify(st.pageView)).not.toContain('Edit page') // the toggle is gone while editing
    await run(s, 'done')
    expect((await state(s)).editing).toBe(false)
    await run(s, 'edit')
    await run(s, 'save', { formData: { slug: target.slug, title: target.title, markdown: '# Saved' } })
    st = await state(s)
    expect(st.editing).toBe(false)
    await run(s, 'edit')
    await run(s, 'close')
    st = await state(s)
    expect(st).toMatchObject({ current: null, editing: false })
  })
  it('edit needs an open page', async () => {
    expect(await refused(run(setup(), 'edit'))).toMatchObject({ status: 404 })
  })
  it('open by title or with an unknown slug changes nothing: pages are addressed by slug only', async () => {
    const s = setup()
    const st0 = await state(s)
    expect(await refused(run(s, 'open', { slug: 'nope' }))).toMatchObject({ status: 404 })
    expect(await refused(run(s, 'open', { slug: st0.pages[2].title }))).toMatchObject({ status: 404 })
    expect((await state(s)).current).toBeNull()
  })
  it('save writes to the page named in the form, even after the open page changed meanwhile', async () => {
    const s = setup()
    const [a, b] = (await state(s)).pages
    await run(s, 'open', { slug: b.slug })
    await run(s, 'edit')
    const form = (find((await state(s)).pageView, 'form') as unknown as { formData: object }).formData // the draft the user is typing into
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
    expect(await refused(run(s, 'save', { formData: { slug: a.slug, title: b.title.toUpperCase(), markdown: 'dup' } }))).toMatchObject({ status: 409, code: 'wiki.title_taken' })
    expect(await refused(run(s, 'save', { formData: { slug: a.slug, title: '  ', markdown: 'empty' } }))).toMatchObject({ status: 400, code: 'validation' })
    st = await state(s)
    expect(st.pages.find((x) => x.slug === a.slug)).toMatchObject({ title: 'New title', markdown: '# Fresh text' })
  })
  it('search filters the list by title and text and an empty query restores it', async () => {
    const s = setup()
    await run(s, 'search', { formData: { query: 'runbook' } })
    const hit = await state(s)
    expect(hit.pageRows.length).toBeGreaterThan(0)
    expect(hit.pageRows.length).toBeLessThan(7)
    expect(hit.pageRows.every((i) => /runbook/i.test(i.page))).toBe(true)
    expect(tabsOf(hit)[0].count).toBe(7) // the tab count is all pages, not the matches
    await run(s, 'search', { formData: { query: '' } })
    expect((await state(s)).pageRows).toHaveLength(7)
  })
  it('link attaches a page, by slug, to the ticket in the body; the panel map reads it', async () => {
    const s = setup()
    const st0 = await state(s)
    expect(st0.byTicket['DEMO-0043'].length).toBeGreaterThanOrEqual(2)
    expect(st0.pageOptions[0]).toEqual({ const: st0.pages[0].slug, title: st0.pages[0].title })
    const free = st0.pages.find((p) => !p.tickets.includes('DEMO-0042'))!
    expect(await refused(run(s, 'link', { ticket: 'DEMO-0042', formData: { page: free.title } }))).toMatchObject({ status: 400 }) // a title is not an address
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
    expect(mara.current).toBeNull()
    expect(mara.pageRows).toHaveLength(7)
    as(s, 'p_sev')
    const sev = await state(s)
    expect(sev.current!.slug).toBe(pages[4].slug)
    expect(sev.pageRows.length).toBeLessThan(7)
  })
  it('a viewer can open and search (changing only their own view) but cannot save or link', async () => {
    const s = setup('p_tom')
    const pages = (await state(s)).pages
    await run(s, 'open', { slug: pages[2].slug })
    await run(s, 'search', { formData: { query: 'tolerance' } })
    const tom = await state(s)
    expect(tom.current!.slug).toBe(pages[2].slug)
    expect(tom.pageRows.length).toBeLessThan(7)
    as(s, 'p_sev')
    expect((await state(s)).current).toBeNull()
    as(s, 'p_tom')
    const body = { ticket: 'DEMO-0042', formData: { slug: pages[0].slug, title: 'Hacked', markdown: 'x', page: pages[0].slug } }
    await expect(run(s, 'save', body)).rejects.toMatchObject({ status: 403 })
    await expect(run(s, 'link', body)).rejects.toMatchObject({ status: 403 })
    await expect(run(s, 'edit')).rejects.toMatchObject({ status: 403 }) // reading is for everyone, editing is not
    expect((await state(s)).pages[0].title).toBe(pages[0].title)
  })
})
