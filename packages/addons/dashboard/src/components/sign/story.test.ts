// Variant B: the button and "What happens next" are worked out from the action, so each case says only what is true.
import { describe, expect, it } from 'vitest'
import { storyOf } from './story'

const new_ = { action: 'new_sequence' as const, title: 'T', type: 'feature' as const, sections: {} }

describe('storyOf', () => {
  it('new ticket, only create: nothing starts', () => {
    const s = storyOf(new_)!
    expect(s.signLabel).toBe('Create ticket')
    expect(s.steps).toEqual([])
    expect(s.access).toBeUndefined()
  })

  it('new ticket + agent access: access, then the agent writes requirements and plan and waits for plan approval', () => {
    const s = storyOf({ ...new_, agent: { hours: 168 } })!
    expect(s.signLabel).toBe('Sign and start agent')
    expect(s.access).toEqual({ length: '7 days' })
    expect(s.steps).toEqual(['You sign.', 'The ticket is created and agents may work for you for 7 days.', 'An agent starts and writes the requirements and a plan for you.', 'It stops and waits: nothing is built until you approve the plan.'])
  })

  it('new ticket + start with access already there: no access is given, the agent still waits for the plan', () => {
    const s = storyOf(new_, { startsAgent: true })!
    expect(s.access).toBeUndefined()
    expect(s.steps.join(' ')).toContain('with the access you already gave')
    expect(s.steps.at(-1)).toContain('until you approve the plan')
    expect(s.steps.join(' ')).not.toContain('access is given')
  })

  it('approve both: one signature locks the requirements and the plan and may start the build', () => {
    const s = storyOf({ action: 'approve_both', key: 'DEMO-1', head_seq: 7 })!
    expect(s.signLabel).toBe('Sign requirements and plan')
    expect(s.steps).toEqual(['You sign.', 'The requirements and the plan are locked as shown.', 'An agent that waits for this approval may start building.'])
  })

  it('approve requirements: the plan still needs approval', () => {
    const s = storyOf({ action: 'approve', key: 'DEMO-1', gate: 'requirements' })!
    expect(s.signLabel).toBe('Sign requirements')
    expect(s.scope).toBe('This signs the requirements only, not the plan.')
    expect(s.steps).toEqual(['You sign.', 'The requirements are locked as shown.', 'The plan still needs your approval, separately.'])
  })

  it('approve plan: building may start', () => {
    const s = storyOf({ action: 'approve', key: 'DEMO-1', gate: 'plan' })!
    expect(s.signLabel).toBe('Sign and start building')
    expect(s.steps[1]).toBe('The agent may start building the plan shown here.')
  })

  it('extend access: the old access is replaced from now, running agents pause', () => {
    const s = storyOf({ action: 'runner_grant', hours: 72, scope: 'workable', extend: 'gr_old' })!
    expect(s.signLabel).toBe('Sign and extend')
    expect(s.access).toEqual({ length: '3 days' })
    expect(s.steps).toEqual(['You sign.', 'The old access ends and the new one starts, for 3 days from now.', 'Running agents stop. Start them again to use the new access.'])
  })

  it('allow access without an earlier one does not talk about replacing anything', () => {
    const s = storyOf({ action: 'runner_grant', hours: 24, scope: 'workable' })!
    expect(s.signLabel).toBe('Sign and allow access')
    expect(s.steps.join(' ')).not.toMatch(/old access|stop/)
  })

  it('an action without a story gets none (the plain Sign stays)', () => {
    expect(storyOf({ action: 'comment', key: 'DEMO-1', message: 'x' })).toBeNull()
    expect(storyOf(null)).toBeNull()
  })
})

describe('r14-P03: starting an agent folds the workspace line and says what is granted', () => {
  it('folds the workspace line for a ticket that starts an agent and for a start grant, not for a plain create', () => {
    expect(storyOf({ action: 'new_sequence', agent: { hours: 72 } } as never)?.foldWorkspace).toBe(true)
    expect(storyOf({ action: 'runner_grant', hours: 72 } as never, { startsAgent: true })?.foldWorkspace).toBe(true)
    expect(storyOf({ action: 'new' } as never)?.foldWorkspace).toBeUndefined()
  })
  it('every start step names the length and says nothing about "access" or a "runner"', () => {
    const s = storyOf({ action: 'new_sequence', agent: { hours: 72 } } as never)!
    expect(s.access).toEqual({ length: '3 days' })
    expect(s.steps.join(' ')).toContain('agents may work for you for 3 days')
    expect(s.steps.join(' ')).not.toMatch(/access|runner/i)
  })
})
