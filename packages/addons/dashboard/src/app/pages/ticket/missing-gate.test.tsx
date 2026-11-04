import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { missingOf } from '@/api/gates'
import { availableActions, gapOf, lackText, primaryAction } from './actions'
import { TicketProgress } from './NextStep'

// B11 (owner run 2, quick task): the plan gate said "Waiting for your approval" with no plan written, and Approve plan
// was refused with gate.incomplete. Core's completeness rule (`missing`) now decides: never offer Approve while it lacks.
const viewer = { person: 'p_sev', role: 'owner' as const, members: [{ person: 'p_sev', name: 'Severin', role: 'owner' as const }], name: (id: string | null | undefined) => (id === 'p_sev' ? 'Severin' : String(id)), ready: true }

async function planWaits() {
  const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
  doc.status = 'open'
  doc.gates.requirements.state = 'approved'
  doc.gates.plan = { ...doc.gates.plan, state: 'pending', approvals: [] }
  expect(doc.tasks.length).toBeGreaterThan(0)
  return doc
}

it('offers Approve plan when core says nothing is missing', async () => {
  const doc = await planWaits()
  doc.gates.plan.missing = []
  expect(availableActions(doc, viewer as never).approve).toContain('plan')
})

it('never offers Approve for a gate whose plan is missing, and says what it waits for', async () => {
  const doc = await planWaits()
  doc.gates.plan.missing = ['plan']
  const av = availableActions(doc, viewer as never)
  expect(av.approve).not.toContain('plan')
  expect(av.requestChanges).not.toContain('plan')
  expect(primaryAction(av)).not.toMatchObject({ kind: 'approve' })
  render(<TicketProgress ticket={doc} viewer={viewer as never} sign={() => {}} jump={() => {}} />)
  expect(screen.queryByRole('button', { name: /Approve plan/ })).toBeNull()
})

it('never offers Approve requirements while acceptance criteria are missing', async () => {
  const doc = await planWaits()
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', approvals: [], missing: ['acceptance'] }
  expect(availableActions(doc, viewer as never).approve).not.toContain('requirements')
})

it('names what else is missing in plain words', () => {
  expect(lackText('plan', ['decisions', 'tasks'])).toBe('Add why the plan is built this way and one step first.')
  expect(lackText('requirements', ['requirements', 'acceptance'])).toBe('Waiting for the agent to write the requirements')
  expect(lackText('requirements', ['context', 'out_of_scope', 'acceptance'])).toBe('Add a short background, what is not included and one check that proves it works first.')
  expect(lackText('plan', ['artifact:design'])).toBe('Add the artifact design first.')
})

it('reads the items of a gate.incomplete refusal', () => {
  expect(missingOf('missing: context, out_of_scope, acceptance')).toEqual(['context', 'out_of_scope', 'acceptance'])
  expect(missingOf('gate.incomplete: missing: plan')).toEqual(['plan'])
  expect(missingOf('something else')).toBeNull()
})

it('gapOf names the first unapproved gate that lacks, in plain words', async () => {
  const doc = await planWaits()
  doc.gates.plan.missing = ['context', 'acceptance']
  expect(gapOf(doc)).toMatchObject({ gate: 'plan', title: 'The plan is not finished yet', text: 'Still missing: a short background and one check that proves it works.' })
  doc.gates.requirements = { ...doc.gates.requirements, state: 'pending', missing: ['requirements'] }
  expect(gapOf(doc)).toMatchObject({ gate: 'requirements', title: 'The requirements are not finished yet', text: 'Still missing: the requirements.' })
  doc.gates.requirements.state = 'approved'
  doc.gates.plan.missing = []
  expect(gapOf(doc)).toBeNull()
})
