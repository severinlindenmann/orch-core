// Mandates, PREVIEW ONLY: the mock's own endpoint. It never writes a workspace or ticket log.
import { describe, expect, it } from 'vitest'
import { createMockStore } from './store'

const setup = () => {
  const s = createMockStore({ persist: false })
  const ws = s.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { s, ws }
}

describe('mandates preview endpoint (mock)', () => {
  it('is off by default and owners only', () => {
    const { s, ws } = setup()
    expect(s.mandatesPreview.state(ws)).toMatchObject({ preview: true, on: false, mandate: null })
    s.setViewer('p_tom')
    expect(s.mandatesPreview.request(ws, { op: 'enable' })).toMatchObject({ ok: false, status: 403 })
  })
  it('the preflight is honest: all four prerequisites are missing in this build', () => {
    const { s, ws } = setup()
    expect(s.mandatesPreview.state(ws).preflight.map((c) => [c.id, c.state])).toEqual([['p1', 'missing'], ['p2', 'missing'], ['p3', 'missing'], ['p4', 'missing']])
  })
  it('issuing needs the preview on, and one mandate at a time', () => {
    const { s, ws } = setup()
    const orch = s.mandatesPreview.state(ws).orchestrators[0].session
    expect(s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, days: 7 })).toMatchObject({ ok: false, status: 409 })
    s.mandatesPreview.request(ws, { op: 'enable' })
    const r = s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, days: 7 })
    expect(r.ok && r.state.mandate).toMatchObject({ id: 'md_3', state: 'active', scope: 'workspace', days: 7 })
    expect(r.ok && r.state.mandate!.decisions).toHaveLength(10)
    expect(s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, days: 7 })).toMatchObject({ ok: false, status: 409 })
  })
  it('the wide scope: what it may do, what stays yours (no chains), a length up to 30 days, renewable', () => {
    const { s, ws } = setup()
    const st = s.mandatesPreview.state(ws)
    expect(st.may.join(' ')).toMatch(/start factory runs, including Deliver \(after the hold window\)/)
    expect(st.always_human).toEqual(expect.arrayContaining(['Settings and policies.', 'Addons: install, update, capabilities.', 'Members and roles.', 'Devices.', 'Relay pairing.', 'Secrets and connections.']))
    expect(st.always_human.join(' ')).toMatch(/never issues, extends or renews one/)
    s.mandatesPreview.request(ws, { op: 'enable' })
    const orch = st.orchestrators[0].session
    expect(s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, days: 31 })).toMatchObject({ ok: false, status: 400 })
    const r = s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, days: 30 })
    const m = r.ok ? r.state.mandate! : null
    expect(m).toMatchObject({ days: 30, revision: 1 })
    // The log is a mix across the workspace, not one epic.
    expect(new Set(m!.decisions.map((d) => d.kind))).toEqual(new Set(['requirements', 'plan', 'verdict', 'unblock', 'code_review', 'factory_enabled', 'permit', 'factory_run', 'grant']))
    expect(m!.refused.map((x) => x.reason)).toEqual(['protected_path', 'veto', 'always_human', 'chain'])
    expect(s.mandatesPreview.request(ws, { op: 'renew', days: 0 })).toMatchObject({ ok: false, status: 400 })
    const n = s.mandatesPreview.request(ws, { op: 'renew', days: 14 })
    const renewed = n.ok ? n.state.mandate! : null
    expect(renewed).toMatchObject({ revision: 2, days: 14 })
    expect(Date.parse(renewed!.expires) - Date.parse(s.now())).toBe(14 * 86_400_000)
    expect(renewed!.revisions.at(-1)!.what).toMatch(/^Renewed on this Mac/)
    s.mandatesPreview.request(ws, { op: 'stop', stop_agents: false })
    expect(s.mandatesPreview.request(ws, { op: 'renew', days: 7 })).toMatchObject({ ok: false, status: 409 })
  })
  it('touches no workspace or ticket log, and Reset turns it off', () => {
    const { s, ws } = setup()
    const wsLog = s.wsEventsOf(ws).length
    const keys = s.ticketKeys(ws)
    const events = keys.map((k) => s.eventsOf(k).length)
    s.mandatesPreview.request(ws, { op: 'enable', seed: true })
    const m = s.mandatesPreview.state(ws).mandate!
    s.mandatesPreview.request(ws, { op: 'review', decision: m.decisions[5].id, review: 'veto' })
    s.mandatesPreview.request(ws, { op: 'stop', stop_agents: true })
    s.mandatesPreview.request(ws, { op: 'revoke' })
    expect(s.wsEventsOf(ws).length).toBe(wsLog)
    expect(keys.map((k) => s.eventsOf(k).length)).toEqual(events)
    s.reset()
    expect(s.mandatesPreview.state(ws)).toMatchObject({ on: false, mandate: null })
  })
})
