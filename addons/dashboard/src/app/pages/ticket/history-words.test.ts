// Polish pass 2026-10-11: History says what happened in words, not event codes or raw widget JSON.
import { describe, expect, it } from 'vitest'
import type { OrchEvent } from '@/api/types'
import { addonOf, eventDetail, refusalWords } from './History'
import type { Viewer } from './shared'

const viewer = { person: 'p_sev', role: 'owner', members: [], name: (id: string | null | undefined) => String(id ?? ''), ready: true } as unknown as Viewer
const ev = (type: string, fields: Record<string, unknown>, actor: OrchEvent['actor'] = { kind: 'agent', id: 'claude-code', session: 's_1', for: 'p_sev' } as OrchEvent['actor']): OrchEvent =>
  ({ v: 2, id: 'e1', seq: 1, at: '2026-10-09T10:00:00Z', type, actor, ...fields }) as OrchEvent

describe('History wording', () => {
  it('an agent.* type is never core: an addon actor stays an addon, and agent.refused is not a core event', () => {
    const addonActor = { kind: 'addon', id: 'sneaky' } as OrchEvent['actor']
    expect(addonOf(ev('agent.refused', { code: 'human_only' }, addonActor))).toBe('sneaky')
    expect(addonOf(ev('agent.anything', {}, addonActor))).toBe('sneaky')
    expect(addonOf(ev('agent.refused', { code: 'human_only' }))).toBe('agent') // no core prefix: a refusal is not a log event
    expect(eventDetail(ev('agent.refused', { code: 'lease.held' }), viewer)).toBe('agent.refused')
  })
  it('says why an agent was refused, in plain words, from the session data', () => {
    expect(refusalWords({ code: 'lease.held', message: 'T2 is leased to s_2.' })).toBe('another session holds that task')
    expect(refusalWords({ code: 'something.new', message: 'Not today.' })).toBe('Not today.')
  })
  it('shows a handoff widget block by its title instead of its JSON', () => {
    const text = 'T1 done.\n\n```orch\n{"widget":"before-after@1","title":"Q2: valid_from column type","data":{}}\n```'
    expect(eventDetail(ev('handoff.written', { text }), viewer)).toBe('Handoff: T1 done.\n\n[widget: Q2: valid_from column type]')
    expect(eventDetail(ev('handoff.written', { text: 'Plain.' }), viewer)).toBe('Handoff: Plain.')
  })
})
