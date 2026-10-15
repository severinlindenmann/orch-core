import { describe, expect, it } from 'vitest'
import { INTERNAL_LINK, parseNode } from './nodes'

const button = (action: string) => parseNode({ type: 'button', label: 'Go', action })

describe('action ids', () => {
  it.each(['share', 'save_settings', 'a.b-c', '9lives', '_private'])('accepts %s', (id) => {
    expect(button(id).ok).toBe(true)
  })
  it.each(['..', '.', '.hidden', '-flag', '', 'a/b', 'x'.repeat(65)])('rejects %j (must start with a letter, digit or underscore)', (id) => {
    expect(button(id).ok).toBe(false)
  })
  it('rejects a bad id in list item actions too', () => {
    expect(parseNode({ type: 'list', items: [{ title: 'x', actions: [{ label: 'Up', action: '..' }] }] }).ok).toBe(false)
  })
})

describe('tables that are often empty say so', () => {
  it('worktrees, start-agent Running, github and records tables carry an `empty` text', async () => {
    const { default: addons } = await import('@/mocks/fixtures/addons.json')
    const { default: catalog } = await import('@/mocks/fixtures/catalog.json')
    const tables: Record<string, string[]> = {}
    const walk = (name: string, n: unknown) => {
      if (Array.isArray(n)) n.forEach((x) => walk(name, x))
      else if (n && typeof n === 'object') {
        const o = n as Record<string, unknown>
        if (o.type === 'table') (tables[name] ??= []).push(typeof o.empty === 'string' ? o.empty : '')
        Object.values(o).forEach((x) => walk(name, x))
      }
    }
    for (const a of [...addons, ...catalog] as { name: string; contributions: { node: unknown }[] }[]) for (const c of a.contributions) walk(a.name, c.node)
    for (const name of ['worktrees', 'start-agent', 'github', 'records']) {
      expect(tables[name]?.length, name).toBeGreaterThan(0)
      for (const e of tables[name]) expect(e, name).not.toBe('')
    }
  })
})

describe('arg keys', () => {
  const withArgs = (args: Record<string, unknown>) => parseNode({ type: 'button', label: 'Go', action: 'share', args })
  it.each(['id', 'schedule_id', 'A1', 'x'.repeat(32)])('accepts %s', (k) => expect(withArgs({ [k]: 1 }).ok).toBe(true))
  it.each(['', '1x', '_x', 'bad key', 'a-b', 'a.b', 'x'.repeat(33), 'ab\u202Ecd', 'Target (target)'])('rejects %j', (k) => expect(withArgs({ [k]: 1 }).ok).toBe(false))
})

describe('INTERNAL_LINK (U3): only an addon page, one row on it, or the ticket list by repo', () => {
  it.each([
    '/addon/repos/repos',
    '/addon/repos/repos?row=web-portal',
    '/addon/repos/repos?tab.repos=structure&row=Acme.Dbt_2',
    '/tickets?repo=acme-energy-dbt',
  ])('allows %s', (h) => expect(INTERNAL_LINK.test(h)).toBe(true))
  it.each([
    '/tickets',
    '/tickets?repo=a&status=done',
    '/tickets?repo=../x',
    '/addon/repos/repos?row=-x',
    '/addon/repos/repos?repo=web-portal',
    '/addon/repos/repos?row=a&tab.repos=x',
    '/addon/repos/repos?tab.repos=structure',
    '/settings/general?row=x',
    '/addon/repos/repos?row=wеb',
  ])('refuses %s', (h) => expect(INTERNAL_LINK.test(h)).toBe(false))
})
