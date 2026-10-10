// Regression tests for the adversarial security review of 2026-10-10 (Codex, 11 findings). Each block names the
// finding it covers; the attack states are modelled in the in-memory store (an agent edit, a manifest/implementation
// mismatch, a ticket that becomes restricted), as in the review's probes.
import { describe, expect, it } from 'vitest'
import { createMockStore, type MockStore } from '@/mocks/store'
import { createMockHandler } from '@/mocks/router'
import { installAndGrant } from '@/test/installAddon'
import { getAddon } from '@/mocks/addons'
import { bindsAddonState, selectContributions } from '@/addon-ui/slots'
import { nodeBudgetProblem } from '@/addon-ui/bindings'
import { parseNode } from '@/addon-ui/nodes'
import type { AddonPackage, TicketDocument, Workspace } from '@/api/types'

const getAddonActions = (name: string) => getAddon(name)?.actions ?? {}

function setup(addon?: string) {
  const s = createMockStore({ persist: false })
  const ws = s.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (addon) installAndGrant(s, ws, addon)
  return { s, ws, api: createMockHandler(s, { latency: false }) }
}
/** Make a ticket restricted to `people` (models a visibility change after the fact). */
const restrict = (s: MockStore, key: string, people: string[]) => {
  ;(s as unknown as { defs: Map<string, { visibility?: unknown }> }).defs.get(key)!.visibility = { restricted: people }
}

describe('#11 every shell-creating terminals path needs the pty grant', () => {
  const noPty = () => {
    const { s, ws } = setup()
    const a = s.workspaces.find((w) => w.id === ws)!.addons.terminals
    a.capabilities = []
    a.granted!.capabilities = []
    return { s, ws }
  }
  it.each(['new', 'open_ticket'])('%s refuses without pty', (id) => {
    const { s, ws } = noPty()
    const r = s.runAddon(ws, 'terminals', id, id === 'open_ticket' ? { ticket: 'DEMO-0043' } : {})
    expect(r).toMatchObject({ ok: false, status: 409, code: 'terminals.no_pty' })
  })
  it('new still works with the grant', () => {
    const { s, ws } = setup()
    expect(s.runAddon(ws, 'terminals', 'new', {})?.ok).toBe(true)
  })
})

describe('#10 an action the installed manifest does not declare is refused', () => {
  it('an implementation left out of the manifest cannot run, whatever the role', () => {
    const { s, ws } = setup()
    const pkg = s.addons.find((p) => p.name === 'estimate')!
    delete pkg.actions!.save_settings
    s.setViewer('p_mara')
    expect(s.runAddon(ws, 'estimate', 'save_settings', { formData: {} })).toMatchObject({ ok: false, status: 403, code: 'addon.undeclared_action' })
    s.setViewer('p_sev')
    expect(s.runAddon(ws, 'estimate', 'save_settings', { formData: {} })).toMatchObject({ ok: false, code: 'addon.undeclared_action' })
  })
  it('every implemented action of every shipped package is declared (nothing falls back to a default)', () => {
    const { s } = setup()
    for (const p of s.addons) {
      const declared = new Set(Object.keys(p.actions ?? {}))
      const upd = p.update?.actions ? new Set(Object.keys(p.update.actions)) : null
      for (const id of Object.keys(getAddonActions(p.name))) {
        expect(declared.has(id), `${p.name}.${id}`).toBe(true)
        if (upd) expect(upd.has(id), `${p.name}.${id} (update)`).toBe(true)
      }
    }
  })
})

describe('#6 a link revocation never names a hidden ticket in an untagged row', () => {
  it('send -> restrict -> revoke -> another viewer sees no key of the hidden ticket', () => {
    const { s, ws } = setup('links')
    const raw = s.addonState(ws, 'links') as { links: { id: string; peer: { name: string } }[]; log: { text: string; ticket?: string }[] }
    restrict(s, 'DEMO-0042', ['p_sev'])
    const peer = raw.links.find((l) => l.id === 'ln_int')!.peer.name
    expect(s.runAddon(ws, 'links', 'revoke', { id: 'ln_int', peer, confirmed: true })?.ok).toBe(true)
    // Every row that names a ticket carries it, so the per-viewer filter applies.
    for (const e of raw.log) if (/DEMO-\d{4}/.test(e.text)) expect(e.ticket, e.text).toBeTruthy()
    // The owner still sees the handoff come back.
    expect(JSON.stringify(s.addonStateView(ws, 'links'))).toContain('DEMO-0042')
    s.setViewer('p_tom')
    expect(s.isVisible('DEMO-0042')).toBe(false)
    expect(JSON.stringify(s.addonStateView(ws, 'links'))).not.toContain('DEMO-0042')
  })
})

describe('#5 package responses carry no runtime decisions', () => {
  it('no package surface returns decisions; a hidden ticket stays out of every one', async () => {
    const { s, api, ws } = setup()
    restrict(s, 'DEMO-0041', ['p_sev'])
    s.setViewer('p_tom')
    expect(s.isVisible('DEMO-0041')).toBe(false)
    expect(s.addonDecisions(ws)).toEqual([])
    const surfaces = [await api('GET', '/api/addons'), await api('GET', `/api/workspaces/${ws}/addons`), await api('GET', `/api/workspaces/${ws}/addon-catalog`)]
    for (const r of surfaces) {
      expect(r.status).toBe(200)
      const text = JSON.stringify(r.json)
      expect(text).not.toContain('DEMO-0041')
      expect(text).not.toContain('Before/after report')
      for (const p of r.json as { decisions?: unknown }[]) expect(p.decisions).toBeUndefined()
    }
  })
  it('an addon operation answers with the public package too', async () => {
    const { s, api, ws } = setup()
    const r = await api('POST', `/api/workspaces/${ws}/addons/publish`, { op: 'disable' })
    expect(r.status).toBe(200)
    expect((r.json as { decisions?: unknown }).decisions).toBeUndefined()
    expect(s.addons.find((p) => p.name === 'publish')!.decisions?.length).toBeGreaterThan(0) // the host keeps them
  })
})

describe('#9 a pathological contribution fails only its own surface, before bindings run', () => {
  const deep = (n: number) => {
    let node: unknown = { type: 'stat', label: 'x', value: '${ticket.key}' }
    for (let i = 0; i < n; i++) node = { type: 'stack', children: [node] }
    return node
  }
  const pkg = (name: string, node: unknown) => ({ name, title: name, version: '1.0.0', description: '', capabilities: [], first_party: false, package_sha256: '', update: null, contributions: [{ slot: 'ticket.panel', id: 'p', title: 'P', node }] }) as unknown as AddonPackage
  const ws = (names: string[]) => ({ addons: Object.fromEntries(names.map((n) => [n, { enabled: true, status: 'active', installed: true, version: '1.0.0', package_sha256: '', capabilities: [], granted: { version: '1.0.0', package_sha256: '', capabilities: [] } }])) }) as unknown as Workspace
  it('12,000 nested stacks neither throw nor take the other addon down', () => {
    const addons = [pkg('evil', deep(12_000)), pkg('good', deep(1))]
    const ctx = { workspace: ws(['evil', 'good']), ticket: { key: 'DEMO-0043' } as TicketDocument }
    expect(() => bindsAddonState(addons[0].contributions[0])).not.toThrow()
    const out = selectContributions(addons, 'ticket.panel', ctx)
    expect(out.map((c) => c.addon)).toEqual(['evil', 'good'])
    expect(parseNode(out[0].node).ok).toBe(false) // drawn as core's "could not be shown" box
    expect(JSON.stringify(out[1].node)).toContain('DEMO-0043')
  })
  it('the budget counts nodes and bytes, not only depth', () => {
    expect(nodeBudgetProblem({ type: 'stack', children: Array.from({ length: 50 }, () => ({ type: 'stack', children: Array.from({ length: 50 }, () => ({ type: 'stack', children: Array.from({ length: 50 }, () => ({ type: 'stat', label: 'x', value: 1 })) })) })) })).toMatch(/nodes/)
    expect(nodeBudgetProblem({ type: 'markdown', text: 'x'.repeat(3_000_000) })).toMatch(/large/)
    expect(nodeBudgetProblem(deep(20))).toBeNull()
  })
})
