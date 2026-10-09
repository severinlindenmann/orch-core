import { describe, expect, it } from 'vitest'
import { parseNode } from './nodes'

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
