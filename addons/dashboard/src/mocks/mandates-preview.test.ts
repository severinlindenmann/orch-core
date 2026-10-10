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
    expect(s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, epic: 'DEMO-0050' })).toMatchObject({ ok: false, status: 409 })
    s.mandatesPreview.request(ws, { op: 'enable' })
    const r = s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, epic: 'DEMO-0050' })
    expect(r.ok && r.state.mandate).toMatchObject({ id: 'md_3', state: 'active', epic: { key: 'DEMO-0050' } })
    expect(r.ok && r.state.mandate!.decisions).toHaveLength(9)
    expect(s.mandatesPreview.request(ws, { op: 'issue', orchestrator: orch, epic: 'DEMO-0050' })).toMatchObject({ ok: false, status: 409 })
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
