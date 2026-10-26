import { describe, expect, it } from 'vitest'
import { SECTIONS_BY_TYPE, type SectionName } from '@/api/sections'
import { accessTooMany, EMPTY, handoffNewTicket, sequencePeople } from './index'

describe('a new ticket through the hand-off', () => {
  it('is one intent: title, type, fields and written text', () => {
    const made = handoffNewTicket({ ...EMPTY, title: ' Fix it ', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } })
    expect(made).toEqual({ action: { action: 'new_sequence', title: 'Fix it', type: 'bug', priority: 'high', labels: ['api'], sections: { requirements: 'It must work' } } })
    expect(handoffNewTicket({ ...EMPTY, title: 'Plain' })).toEqual({ action: { action: 'new_sequence', title: 'Plain', type: 'feature', sections: {} } })
  })
  it('says what a signed new ticket does not carry yet instead of dropping it', () => {
    for (const patch of [{ visibility: { restricted: ['p_x'] } }, { due: '2026-11-01' }]) {
      expect(handoffNewTicket({ ...EMPTY, title: 'T', ...patch })).toHaveProperty('problem')
    }
  })
  it('carries acceptance criteria and people in the same intent (N3)', () => {
    const me = 'p_' + 'a'.repeat(32)
    const quinn = 'p_' + 'b'.repeat(32)
    const sam = 'p_' + 'c'.repeat(32)
    const made = handoffNewTicket(
      { ...EMPTY, title: 'T', acceptance: [' It opens ', '', 'It saves'], people: { owner: quinn, assignees: [sam, quinn, sam], reviewers: [me] } },
      me,
    )
    expect(made).toEqual({
      action: { action: 'new_sequence', title: 'T', type: 'feature', sections: {}, acceptance: ['It opens', 'It saves'], people: { owner: quinn, assignees: [quinn, sam], reviewers: [me] } },
    })
  })
  it('does not sign the creator as the owner again, and sends no people when none are chosen', () => {
    const me = 'p_' + 'a'.repeat(32)
    expect(handoffNewTicket({ ...EMPTY, title: 'T', people: { owner: me, assignees: [], reviewers: [] } }, me)).toEqual({
      action: { action: 'new_sequence', title: 'T', type: 'feature', sections: {} },
    })
  })
  it('says so when one signing run cannot carry that many changes', () => {
    const many = { ...EMPTY, title: 'T', acceptance: Array.from({ length: 8 }, (_, i) => `c${i}`), sections: { summary: 'a', context: 'b', requirements: 'c', out_of_scope: 'd', plan: 'e', decisions: 'f', verification: 'g', current_state: 'h' } }
    expect(handoffNewTicket(many as never)).toHaveProperty('problem')
    expect(handoffNewTicket({ ...EMPTY, title: 'T', acceptance: Array.from({ length: 9 }, (_, i) => `c${i}`) })).toHaveProperty('problem')
  })
})

describe('F-712-B: the step limit with and without the agent access', () => {
  it('a draft that fits alone but not with the access is "too many"; fewer or no access is not', () => {
    const crit = (n: number) => Array.from({ length: n }, (_, i) => `c${i}`)
    const names = (Object.keys(SECTIONS_BY_TYPE.feature) as SectionName[]).filter((n) => SECTIONS_BY_TYPE.feature[n] !== 'absent')
    const draft = (sections: number, criteria: number) => ({ ...EMPTY, title: 'T', priority: 'high' as const, sections: Object.fromEntries(names.slice(0, sections).map((n) => [n, 'x'])), acceptance: crit(criteria) })
    // Steps: ticket + fields + sections + criteria. Find the draft of exactly 16 (fits alone; the access makes it 17).
    const sections = 6
    const edge = draft(sections, 14 - sections)
    expect('problem' in handoffNewTicket(edge, 'p', null)).toBe(false)
    expect(accessTooMany(edge, 'p', 24)).toBe(true)
    expect(accessTooMany(draft(sections, 14 - sections - 1), 'p', 24)).toBe(false)
  })
})

describe('#1070 the real draft to sequence step', () => {
  const me = 'p_' + '1'.repeat(32)
  it('the creator as owner, assignee and reviewer signs no owner step', () => {
    const people = { owner: me, assignees: [me], reviewers: [me] }
    expect(sequencePeople(people as never, me)).toEqual({ assignees: [me], reviewers: [me] })
    const d = { ...EMPTY, title: 'T', size: 1, people, sections: { requirements: 'It works.' } } as never
    const made = handoffNewTicket(d, me)
    expect('action' in made && (made.action as { people?: object }).people).toEqual({ assignees: [me], reviewers: [me] })
  })
  it('another person as owner keeps an owner step', () => {
    const other = 'p_' + '2'.repeat(32)
    expect(sequencePeople({ owner: other, assignees: [], reviewers: [] } as never, me)).toEqual({ owner: other })
  })
})
