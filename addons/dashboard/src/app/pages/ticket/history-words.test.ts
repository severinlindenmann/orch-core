// Polish pass 2026-10-11: History says what happened in words, not event codes or raw widget JSON.
import { describe, expect, it } from 'vitest'
import type { OrchEvent } from '@/api/types'
import { addonOf, eventDetail } from './History'
import type { Viewer } from './shared'

const viewer = { person: 'p_sev', role: 'owner', members: [], name: (id: string | null | undefined) => String(id ?? ''), ready: true } as unknown as Viewer
const ev = (type: string, fields: Record<string, unknown>, actor: OrchEvent['actor'] = { kind: 'agent', id: 'claude-code', session: 's_1', for: 'p_sev' } as OrchEvent['actor']): OrchEvent =>
  ({ v: 2, id: 'e1', seq: 1, at: '2026-10-09T10:00:00Z', type, actor, ...fields }) as OrchEvent

describe('History wording', () => {
  it('says why an agent was refused, in plain words, and counts it as core (not an addon)', () => {
    const e = ev('agent.refused', { code: 'lease.held', message: 'T2 is leased to s_2.' })
    expect(eventDetail(e, viewer)).toBe('Refused: another session holds that task')
    expect(addonOf(e)).toBeNull()
    expect(eventDetail(ev('agent.refused', { code: 'something.new', message: 'Not today.' }), viewer)).toBe('Refused: Not today.')
  })
  it('shows a handoff widget block by its title instead of its JSON', () => {
    const text = 'T1 done.\n\n```orch\n{"widget":"before-after@1","title":"Q2: valid_from column type","data":{}}\n```'
    expect(eventDetail(ev('handoff.written', { text }), viewer)).toBe('Handoff: T1 done.\n\n[widget: Q2: valid_from column type]')
    expect(eventDetail(ev('handoff.written', { text: 'Plain.' }), viewer)).toBe('Handoff: Plain.')
  })
})
