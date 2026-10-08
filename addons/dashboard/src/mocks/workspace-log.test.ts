import { beforeEach, describe, expect, it } from 'vitest'
import { createMockStore } from './store'
import { STORAGE_KEY } from './persist'

describe('workspace log', () => {
  beforeEach(() => localStorage.clear())

  it('derives a role change from member.role_changed', () => {
    const s = createMockStore({ persist: false })
    const ws = s.workspaces[0].id
    s.appendWs(ws, { type: 'member.role_changed', person: 'p_tom', role: 'member' })
    expect(s.workspaceList()[0].members.find((m) => m.person === 'p_tom')?.role).toBe('member')
  })

  it('persists workspace events and reloads them', () => {
    const a = createMockStore({ persist: true })
    a.appendWs(a.workspaces[0].id, { type: 'addon.disabled', name: 'wiki' })
    const b = createMockStore({ persist: true })
    expect(b.workspaceList()[0].addons.wiki.enabled).toBe(false)
  })

  it('discards v1 storage instead of crashing', () => {
    localStorage.setItem('orch-mock', JSON.stringify({ events: { 'DEMO-0043': [{ junk: true }] } }))
    localStorage.setItem(STORAGE_KEY, '{not json')
    expect(() => createMockStore({ persist: true })).not.toThrow()
    expect(createMockStore({ persist: true }).ticket('DEMO-0043')?.key).toBe('DEMO-0043')
    expect(localStorage.getItem('orch-mock')).toBeNull()
  })

  it('drops malformed persisted events on load instead of crashing the folds', () => {
    const seed = createMockStore({ persist: false })
    const ws = seed.workspaces[0].id
    const good = { v: 2, id: 'x1', seq: 1, at: '2026-10-09T11:31:00Z', type: 'workspace.renamed', actor: { kind: 'person', id: 'p_sev' }, name: 'Renamed' }
    const goodView = { ...good, id: 'x2', seq: 2, type: 'view.saved', view: 'v_9', name: 'Kept', shared: true, params: {} }
    const bad = [
      { ...good, id: 'b1', type: 'view.saved', actor: null, view: 'v_bad', name: 'Bad' },
      { ...good, id: 'b2', type: 'view.saved', actor: { kind: 'person' }, view: 'v_bad2', name: 'Bad2' },
      { ...good, id: 'b3', type: 42 },
      null,
      'junk',
    ]
    const ticketGood = { v: 2, id: 't1', seq: 999, at: '2026-10-09T11:32:00Z', type: 'log.added', actor: { kind: 'person', id: 'p_sev' }, text: 'kept' }
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ v: 2, ticketEvents: { 'DEMO-0043': [ticketGood, { type: 'log.added' }, null] }, created: {}, wsEvents: { [ws]: [good, ...bad, goodView] }, addonState: {} }),
    )
    const s = createMockStore({ persist: true })
    expect(s.workspaceList()[0].name).toBe('Renamed')
    expect(s.views(ws).map((v) => v.name)).toContain('Kept')
    expect(s.views(ws).map((v) => v.name)).not.toContain('Bad')
    expect(s.wsEventsOf(ws)).toHaveLength(2)
    expect(s.eventsOf('DEMO-0043').filter((e) => e.type === 'log.added' && e.text === 'kept')).toHaveLength(1)
    expect(() => s.ticket('DEMO-0043')).not.toThrow()
  })

  it('reset() drops workspace events too', () => {
    const s = createMockStore({ persist: false })
    s.appendWs(s.workspaces[0].id, { type: 'workspace.renamed', name: 'X' })
    s.reset()
    expect(s.workspaceList()[0].name).not.toBe('X')
  })

  it('numbers workspace events per workspace and defaults the actor to the viewer', () => {
    const s = createMockStore({ persist: false })
    const [a, b] = s.workspaces
    const e1 = s.appendWs(a.id, { type: 'workspace.renamed', name: 'A1' })
    const e2 = s.appendWs(a.id, { type: 'workspace.renamed', name: 'A2' })
    const e3 = s.appendWs(b.id, { type: 'workspace.renamed', name: 'B1' })
    expect([e1.seq, e2.seq, e3.seq]).toEqual([1, 2, 1])
    expect(e1.actor).toEqual({ kind: 'person', id: s.viewer })
    expect(s.wsEventsOf(a.id)).toHaveLength(2)
  })

  it('folds gate policy, member add/remove, addon install/uninstall; ignores unknown types', () => {
    const s = createMockStore({ persist: false })
    const ws = s.workspaces[0].id
    s.appendWs(ws, { type: 'gate.policy_set', gate: 'plan', approvers: 'maintainer', count: 2 })
    s.appendWs(ws, { type: 'member.added', person: 'p_new', name: 'New', role: 'viewer' })
    s.appendWs(ws, { type: 'member.removed', person: 'p_tom' })
    s.appendWs(ws, { type: 'addon.installed', name: 'extra', version: '1.0.0' })
    s.appendWs(ws, { type: 'addon.uninstalled', name: 'wiki' })
    s.appendWs(ws, { type: 'bogus.type' as never })
    const w = s.workspaceList()[0]
    expect(w.gates.plan).toEqual({ approvers: 'maintainer', count: 2 })
    expect(w.members.map((m) => m.person)).toEqual(['p_sev', 'p_mara', 'p_new'])
    expect(w.addons.extra).toEqual({ enabled: false, status: 'needs_grant', installed: true, granted: null, version: '1.0.0', package_sha256: '', capabilities: [] })
    expect(w.addons.wiki).toBeUndefined()
  })

  it('folds grants: seed grant, issue and revoke', () => {
    const s = createMockStore({ persist: false })
    const ws = s.workspaces[0].id
    expect(s.grants(ws).map((g) => g.id)).toEqual(['gr_01J9Z8', 'gr_01J9Y4', 'gr_01J9C1', 'gr_01J9X2'])
    s.appendWs(ws, { type: 'grant.issued', grant: 'gr_new', person: 'p_mara', scope: 'ci', until: '2026-10-10T00:00:00Z' })
    s.appendWs(ws, { type: 'grant.revoked', grant: 'gr_01J9Z8' })
    const g = s.grants(ws)
    expect(g.find((x) => x.id === 'gr_new')?.scope).toBe('ci')
    expect(g.find((x) => x.id === 'gr_01J9Z8')?.revoked?.by).toBe(s.viewer)
  })
})
