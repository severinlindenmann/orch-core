import { fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { APPROVAL_PRESETS, PRESET_LABEL, policyUnmet, presetOf, presetPolicy } from '@/api/settings'
import * as gesture from '@/components/sign/trustedPress'
import { DO, MARA, OWNER, READS, TOM, policy, settingsOf, useFakeHost, workspace, show } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: 'a'.repeat(32), features: [...READS, ...DO, 'click'], click: { custody: 'convenience', enabled: true, available: true } })])
afterEach(() => vi.restoreAllMocks())
const prepared = () => state.host.clickPrepares().map((c) => c.body)
/** The review's outcome button, pressed for real (fireEvent is untrusted: trustedPress stands in): the signer prepares and its View shows. */
const press = async (dialog: HTMLElement, name: string | RegExp) => {
  vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
  fireEvent.click(within(dialog).getByRole('button', { name }))
  await within(dialog).findByTestId('click-panel', {}, T)
}
const gates = (over: Record<string, unknown> = {}) => ({
  requirements: policy({ approvers: ['owner'], count: 1 }),
  plan: policy({ approvers: ['maintainer', 'owner'], count: 1 }),
  verify: policy({ approvers: ['reviewers'], not: ['assignees'] }),
  code: policy({ approvers: ['maintainer', 'owner'], not: ['assignees'], applies: 'off', independent: true }),
  ...over,
})

it('the four presets stand for exact policies, and a stored policy is recognised as one of them or as custom', () => {
  expect(APPROVAL_PRESETS.map((k) => PRESET_LABEL[k])).toEqual(['An owner', 'An owner or maintainer', 'Two people (owners or maintainers)', 'Someone other than who did the work'])
  const base = policy()
  expect(presetPolicy('owner', base)).toEqual({ approvers: ['owner'], count: 1, not: [], applies: 'all', independent: false })
  expect(presetPolicy('two', base)).toEqual({ approvers: ['maintainer', 'owner'], count: 2, not: [], applies: 'all', independent: false })
  expect(presetPolicy('independent', base)).toEqual({ approvers: ['maintainer', 'owner'], count: 1, not: ['assignees'], applies: 'all', independent: true })
  for (const k of APPROVAL_PRESETS) expect(presetOf(presetPolicy(k, base))).toBe(k)
  expect(presetOf(policy({ approvers: ['reviewers'], not: ['assignees'] }))).toBeNull()
  // Where a rule applies is not part of the choice.
  expect(presetOf({ ...presetPolicy('owner', base), applies: ['feature'] })).toBe('owner')
})

it('a step can never be met when it needs more people than can approve', () => {
  const people = [{ person: 'a', role: 'owner' as const }, { person: 'b', role: 'viewer' as const }]
  expect(policyUnmet(policy({ approvers: ['owner'], count: 2 }), people)).toEqual({ needs: 2, has: 1 })
  expect(policyUnmet(policy({ approvers: ['owner', 'maintainer', 'member'], count: 1 }), people)).toBeNull()
  expect(policyUnmet(policy({ approvers: ['owner'], count: 2, applies: 'off' }), people)).toBeNull()
  expect(policyUnmet(policy({ approvers: ['owner'], count: 1, not: ['owner'] }), people)).toEqual({ needs: 1, has: 0 })
})

it('Approvals: three questions with a preset each; choosing one signs the policy it stands for', async () => {
  state.host.workspaces[0].settings = settingsOf({ gates: gates() } as never)
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  expect(await screen.findByRole('heading', { name: 'Who approves what?' }, T)).toBeInTheDocument()
  for (const step of ['Who approves the requirements?', 'Who approves the plan?', 'Who checks the finished work?']) expect(screen.getByLabelText(step)).toBeInTheDocument()
  expect(screen.queryByLabelText('Code review')).not.toBeInTheDocument() // a switch, not a question
  const plan = screen.getByLabelText('Who approves the plan?')
  expect(plan).toHaveValue('owner_or_maintainer')
  expect(screen.getByLabelText('Who checks the finished work?')).toHaveValue('custom') // approvers = reviewers is a custom rule
  expect(screen.getByLabelText('Who approves the requirements?')).toHaveValue('owner')
  await user.selectOptions(plan, 'two')
  await press(await screen.findByRole('dialog', {}, T), /^Change who approves/)
  expect(prepared()).toEqual([{ action: 'policy_set', gate: 'plan', policy: { approvers: ['maintainer', 'owner'], count: 2, not: [], applies: 'all', independent: false } }])
  expect(state.host.posts()).toEqual([])
})

it('Approvals: "Two people" is not offered while fewer than two people can approve; a sole owner sees "An owner (you)"', async () => {
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.filter((m) => m.person === OWNER || m.person === TOM)
  show('/w/DEMO/settings/gates')
  const requirements = await screen.findByLabelText('Who approves the requirements?', {}, T)
  expect(within(requirements).getByRole('option', { name: 'An owner (you)' })).toBeInTheDocument()
  expect(within(requirements).getByRole('option', { name: 'Two people (owners or maintainers)' })).toBeDisabled()
})

it('Approvals: a stored policy that can never be met says so in words', async () => {
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.filter((m) => m.person === OWNER)
  state.host.workspaces[0].settings = settingsOf({ gates: gates({ plan: policy({ approvers: ['owner'], count: 2 }) }) } as never)
  show('/w/DEMO/settings/gates')
  const unmet = await screen.findByTestId('approval-plan-unmet', {}, T)
  expect(unmet).toHaveTextContent('2 approvals required; only 1 person qualifies.')
  expect(within(unmet).getByRole('button', { name: 'Change rule' })).toBeInTheDocument()
  expect(screen.queryByTestId('approval-requirements-unmet')).not.toBeInTheDocument()
})

it('Approvals: the code review switch turns the gate on for every ticket, never approved by the assignees', async () => {
  state.host.workspaces[0].settings = settingsOf({ gates: gates() } as never)
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  const sw = await screen.findByRole('switch', { name: 'Require a code review before changes are added' }, T)
  expect(sw).not.toBeChecked()
  await user.click(sw)
  await press(await screen.findByRole('dialog', {}, T), 'Require a code review before changes are added')
  expect(prepared()).toEqual([{ action: 'policy_set', gate: 'code', policy: { approvers: ['maintainer', 'owner'], count: 1, not: ['assignees'], applies: 'all', independent: true } }])
  expect(state.host.posts()).toEqual([])
})

it('Approvals: a maintainer reads the choices and cannot change them', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  show('/w/DEMO/settings/gates')
  expect(await screen.findByLabelText('Who approves the plan?', {}, T)).toBeDisabled()
})
