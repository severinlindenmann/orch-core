import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { DASHBOARD_EXECUTION_LINE } from '@/api/quickRecords'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { queryClient } from '@/production/Shell'
import { DO, MARA, OWNER, READS, TOM, clickOn, prepareByClick, roster, settingsOf, show, useFakeHost, workspace } from '@/test/hostHarness'

// The production app on a fake server that speaks the M6 routes (quick tasks, records, the runner line).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const FEATURES = [...READS, ...DO, 'actions', 'quick', 'quick_on', 'records']
const URL_PUSH = 'https://git.example.test/me/records.git'
const row = (key: string, title: string, status: string, quick: { commits: number | null; files: number | null; over: boolean | null } | null, labels: string[] = ['quick']) => ({
  key, uid: 'U' + key, title, type: 'chore', priority: null, size: null, status, labels, parent: null, owner: null, assignees: [], claim: null,
  turn: { who: 'nobody', why: '' }, progress: null, open_questions: 0, blocking_questions: null, restricted: false, awaiting_gate: null, addons: {}, updated_at: '2026-10-12T09:00:00Z', quick,
})
const active = {
  available: true,
  rows: [
    row('DEMO-0010', 'Fix the typo in README', 'open', { commits: 1, files: 1, over: false }),
    row('DEMO-0014', 'Also tagged docs', 'open', { commits: 1, files: 1, over: false }, ['quick', 'docs', 'web']),
    row('DEMO-0011', 'Rename the stale fixture', 'in-progress', { commits: 4, files: 7, over: true }),
    row('DEMO-0012', 'No repo linked yet', 'backlog', { commits: null, files: null, over: null }),
  ],
  next_page: null,
  total: 4,
}
const finished = { available: true, rows: [row('DEMO-0013', 'Drop the old logo', 'done', { commits: 1, files: 1, over: false })], next_page: null, total: 1 }
const quick = (q: URLSearchParams) => (q.getAll('status').includes('done') ? finished : active)
const status = (over: Record<string, unknown> = {}) => ({
  available: true,
  configured: true,
  repo: { state: 'ok', problem: null, problem_hidden: false },
  pending: { events: 12, tickets: [{ key: 'DEMO-0001', events: 7 }], more: false, workspace_events: 5 },
  last: { commit: 'c'.repeat(40), at: '2026-10-12T08:00:00Z', by: OWNER, ws_seq: 40, pushed_at: null },
  last_noted: { by: OWNER, at: '2026-10-12T08:00:00Z' },
  auto: { on: true, every_min: 15, push: false },
  remote: { url: URL_PUSH, url_hidden: false, private: false, export: 'ok' },
  ...over,
})
const state = useFakeHost(() => [workspace({ id: A, features: FEATURES, quick, records: status(), runner: { connected: true, enabled: true, terminals: true } })])
const other = (...c: string[]) => state.host.calls.filter((x) => c.some((p) => x.path.includes(p)))

// ---------------------------------------------------------------- quick tasks

it('lists the quick tasks in plain status words, with a chip only for the one the host measured as too big', async () => {
  show('/w/DEMO/quick')
  const list = await screen.findByRole('list', { name: 'Active quick tasks' }, T)
  const items = within(list).getAllByRole('listitem')
  expect(items).toHaveLength(4)
  const big = items.find((i) => i.textContent?.includes('DEMO-0011'))!
  expect(within(big).getByTestId('quick-over')).toHaveTextContent('Bigger than a quick task')
  for (const i of items) expect(within(i).getByTestId('quick-status')).toHaveTextContent('Not started')
  expect(list).not.toHaveTextContent(/commits|size not measured|too big/) // no size is asked or shown
  expect(items.filter((i) => within(i).queryByTestId('quick-over'))).toHaveLength(1)
  expect(await screen.findByRole('tab', { name: /^Finished\s*1/ }, T)).toBeInTheDocument()
  expect(other('/quick')[0].path).not.toContain('max_commits') // the host measures: nothing is sent
})

it('there is no limits box: the quick limits and their browser storage are gone', async () => {
  show('/w/DEMO/quick')
  await screen.findByRole('list', { name: 'Active quick tasks' }, T)
  expect(screen.queryByTestId('quick-limits')).toBeNull()
  expect(screen.queryByText(/When is a quick task too big/)).toBeNull()
  expect(screen.queryByTestId('quick-one-signature')).toBeNull()
  expect(screen.queryByTestId('quick-what-you-sign')).toBeNull()
})

it('the Finished tab asks for the finished statuses', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.click(await screen.findByRole('tab', { name: /^Finished/ }, T))
  const list = await screen.findByRole('list', { name: 'Finished quick tasks' }, T)
  expect(list).toHaveTextContent('Drop the old logo')
  expect(within(list).getByTestId('quick-status')).toHaveTextContent('Finished')
  expect(within(list).queryByRole('button', { name: /^More for/ })).toBeNull() // nothing to close
})

it('an absent cap or op is an honest "not served", and without the feature the page is not reached', async () => {
  state.host.workspaces[0].quick = { available: false, rows: null, next_page: null, total: null }
  show('/w/DEMO/quick')
  expect(await screen.findByTestId('quick-unavailable', {}, T)).toHaveTextContent('does not serve quick tasks')
})

it('no quick feature: the route goes to the board and the sidebar has no entry', async () => {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  show('/w/DEMO/quick')
  await screen.findByRole('navigation', { name: 'Main' }, T)
  expect(screen.queryByRole('link', { name: 'Quick tasks' })).toBeNull()
  expect(other('/quick')).toEqual([])
})

it('a viewer sees the list but no add form and no menu', async () => {
  state.host.workspaces[0].role = 'viewer'
  state.host.workspaces[0].roster = roster().map((m) => (m.person === OWNER ? { ...m, role: 'viewer' as const } : m))
  show('/w/DEMO/quick')
  await screen.findByRole('list', { name: 'Active quick tasks' }, T)
  expect(screen.queryByLabelText('What needs doing?')).toBeNull()
  expect(screen.queryByRole('button', { name: /^More for/ })).toBeNull()
})

it('the form is one input and one button; details and checks are folded', async () => {
  show('/w/DEMO/quick')
  const field = await screen.findByLabelText('What needs doing?', {}, T)
  expect(field).toBeInTheDocument()
  expect(screen.getByText('Add details or checks').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByRole('button', { name: 'Sign and create task' })).toBeDisabled() // no agent can start on this fake host
  expect(screen.queryByRole('button', { name: 'Create without starting' })).toBeNull()
})

it('creating is ONE One click signature with the whole task: title, details, checks', async () => {
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Fix the typo in CONTRIBUTING')
  await user.click(screen.getByText('Add details or checks'))
  await user.type(screen.getByLabelText('Details (optional)'), 'It says teh.')
  await user.type(screen.getByLabelText('Check 1'), 'The typo is gone')
  await user.type(screen.getByLabelText('Check 1 command'), 'test -z "$(grep -w teh CONTRIBUTING.md)"')
  await user.click(screen.getByRole('button', { name: 'Sign and create task' }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Sign and create task')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{
    action: 'quick_start',
    title: 'Fix the typo in CONTRIBUTING',
    description: 'It says teh.',
    checks: [{ text: 'The typo is gone', cmd: 'test -z "$(grep -w teh CONTRIBUTING.md)"' }],
  }])
  expect(state.host.posts()).toEqual([]) // no terminal hand-off
}, 30000)

it('without One click nothing is prepared: the review says so and links to turning it on', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Fix the typo in CONTRIBUTING')
  await user.click(screen.getByRole('button', { name: 'Sign and create task' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(dialog).toHaveTextContent('One click is not ready on this computer.')
  expect(within(dialog).getByRole('link', { name: 'Turn on One click' })).toBeInTheDocument()
  expect(state.host.posts()).toEqual([])
  expect(other('/click/prepare')).toEqual([])
})

it('with an agent runner and no access: "Create without starting" signs the task without the access', async () => {
  state.host.workspaces[0].features = [...FEATURES, 'agent_start']
  state.host.workspaces[0].settings = settingsOf({ grant_hours: 72 })
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Water the plants')
  await user.click(await screen.findByRole('button', { name: 'Create without starting' }, T))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Sign and create task')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'quick_start', title: 'Water the plants' }])
}, 30000)

it('with an agent runner and no access the review carries the access length as a select (capped by the workspace)', async () => {
  state.host.workspaces[0].features = [...FEATURES, 'agent_start']
  state.host.workspaces[0].settings = settingsOf({ grant_hours: 72 })
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Water the plants')
  await user.click(await screen.findByRole('button', { name: 'Sign and start task' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  const select = await within(dialog).findByLabelText('Give agents access for', {}, T)
  expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual(['8 hours', '1 day', '3 days'])
  expect(state.host.clickPrepares()).toEqual([]) // nothing is prepared until the choice is made and the outcome pressed
  await user.selectOptions(select, '24') // the default is 8 hours (D1/D2): another length re-keys the review, so look it up again
  await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: 'Sign and start task' }))
  await waitFor(() => expect(state.host.clickPrepares()).toHaveLength(1), T)
  expect(state.host.calls.filter(c => c.path.endsWith('/click/sign'))).toEqual([])
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'quick_start', title: 'Water the plants', agent: { hours: 24 } }])
}, 30000)

it('adding where click-to-sign is on goes through the click routes, not a hand-off, with no terminal choice', async () => {
  state.host.workspaces[0].features = [...FEATURES, 'click']
  state.host.workspaces[0].click = { custody: 'convenience', enabled: true, available: true }
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Remove the dead import')
  await user.click(screen.getByRole('button', { name: 'Sign and create task' }))
  const first = await screen.findByRole('dialog', {}, T)
  await within(first).findByRole('button', { name: /^Sign( |$)/ }, T)
  expect(state.host.posts()).toEqual([]) // no terminal hand-off was prepared
  expect(within(first).queryByRole('button', { name: /terminal/i })).toBeNull()
  expect(within(first).queryByRole('button', { name: /phone/i })).toBeNull()
})

it('the checks are editable: up to four, and the limit is explained', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.type(await screen.findByLabelText('What needs doing?', {}, T), 'Make a giraffe chart')
  await user.click(screen.getByText('Add details or checks'))
  await user.type(screen.getByLabelText('Check 1'), 'Read the data')
  for (let i = 0; i < 3; i++) await user.click(screen.getByRole('button', { name: 'Add a check' }))
  expect(screen.getByLabelText('Check 4')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add a check' })).toBeNull()
  expect(screen.getByText(/up to 4 checks/)).toBeInTheDocument()
  await user.type(screen.getByLabelText('Check 2'), 'Draw the chart')
  await user.click(screen.getByRole('button', { name: 'Remove check 1' }))
  expect(screen.queryByLabelText('Check 4')).toBeNull()
  expect(screen.getByLabelText('Check 1')).toHaveValue('Draw the chart')
})

it('a title that is empty or has a line break is never submitted', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  const field = await screen.findByLabelText('What needs doing?', {}, T)
  const go = () => screen.getByRole('button', { name: 'Sign and create task' })
  expect(go()).toBeDisabled()
  await user.type(field, '   ')
  expect(go()).toBeDisabled()
  await user.clear(field)
  await user.type(field, 'x'.repeat(121))
  expect(go()).toBeDisabled()
  expect(screen.getByRole('alert')).toHaveTextContent('at most 120')
})

it('Close (in the row menu) signs the close for that key', async () => {
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await user.click(await screen.findByRole('button', { name: 'More for DEMO-0010' }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Close' }, T))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Close DEMO-0010')
  expect(state.host.clickPrepares()[0].body).toMatchObject({ action: 'close', key: 'DEMO-0010' })
})

it('Make a normal ticket (in the row menu) reads fresh labels and sets exactly the remaining labels', async () => {
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  show('/w/DEMO/quick')
  await screen.findByRole('list', { name: 'Active quick tasks' }, T)
  state.host.override = (path, method) => path.endsWith('/tickets/DEMO-0014') && method === 'GET' ? Response.json({ ...ticket, key: 'DEMO-0014', labels: ['quick', 'docs', 'web', 'new-label'] }) : undefined
  await user.click(await screen.findByRole('button', { name: 'More for DEMO-0014' }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Make a normal ticket' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(dialog).toHaveTextContent('Labels removed: quick')
  expect(dialog).toHaveTextContent('Labels kept: docswebnew-label')
  await prepareByClick(dialog, 'Make DEMO-0014 a normal ticket')
  expect(state.host.clickPrepares()[0].body).toMatchObject({ action: 'details', key: 'DEMO-0014', labels: ['docs', 'web', 'new-label'] })
})

// ---------------------------------------------------------------- records

it('records: status, the export warning and the commands for what only a terminal does, verbatim', async () => {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  const warning = await screen.findByTestId('records-export-warning', {}, T)
  expect(warning.textContent).toBe(`Push publishes every ticket in this workspace to the remote configured in the records repository (last known: ${URL_PUSH}), restricted ones included.`)
  expect(screen.queryByTestId('records-private-line')).toBeNull() // not marked private
  expect(screen.getByText('12 events in 1 ticket, plus 5 workspace events')).toBeInTheDocument()
  expect(screen.getByText('on, every 15 minutes, without push')).toBeInTheDocument()
  for (const c of ['orch records auto on', 'orch records auto on --push']) expect(screen.getByText(c)).toBeInTheDocument()
  // #1198 C15: the remote steps are buttons with a review, not commands.
  for (const c of ['orch records remote private', 'orch records push --initial']) expect(screen.queryByText(c)).toBeNull()
  expect(screen.queryByRole('button', { name: /auto on|runner enable/i })).toBeNull()
})

it('records: a private remote is labelled as a historical note', async () => {
  state.host.workspaces[0].records = status({ remote: { url: URL_PUSH, url_hidden: false, private: true, export: 'ok' } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect((await screen.findByTestId('records-private-line', {}, T)).textContent).toBe(`Last noted as private: ${URL_PUSH}.`)
})

it('records: a push blocked for convenience readers is said, and Commit and push is not offered', async () => {
  state.host.workspaces[0].records = status({ remote: { url: URL_PUSH, url_hidden: false, private: false, export: 'blocked_convenience' } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByTestId('records-export-blocked', {}, T)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save now' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Commit and push' })).toBeNull()
})

it('records: hidden characters in the URL are escaped by visible.tsx, never repaired', async () => {
  const bad = 'https://git.example.test/a‮b.git'
  state.host.workspaces[0].records = status({ remote: { url: bad, url_hidden: false, private: false, export: 'ok' } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  const warning = await screen.findByTestId('records-export-warning', {}, T)
  expect(warning.textContent).toContain('a\\u{202e}b.git')
  expect(warning.textContent).not.toContain('‮')
})

it('records: a URL the server withheld says so and never invents one', async () => {
  state.host.workspaces[0].records = status({ remote: { url: null, url_hidden: true, private: null, export: null }, auto: { on: false, every_min: 15, push: true } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  const warning = await screen.findByTestId('records-export-warning', {}, T)
  expect(warning).toHaveTextContent('cannot be shown here')
  expect(screen.queryByRole('button', { name: 'Commit and push' })).toBeNull()
})

it('records: commit now posts exactly {push:false} and shows the result', async () => {
  const user = userEvent.setup()
  state.host.override = (path, method) => (path.endsWith('/records/commit') && method === 'POST' ? Response.json({ ok: true, commit: 'd'.repeat(40), pushed: false }) : undefined)
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Save now' }, T))
  expect(await screen.findByTestId('records-result', {}, T)).toHaveTextContent(`Committed ${'d'.repeat(40)}, not pushed`)
  expect(state.host.calls.find((c) => c.path.endsWith('/records/commit'))?.body).toEqual({ push: false })
})

it('records: commit and push shows the export warning first and posts {push:true} only after confirming', async () => {
  state.host.workspaces[0].records = status({ remote: { url: URL_PUSH, url_hidden: false, private: true, export: 'ok' } })
  const user = userEvent.setup()
  state.host.override = (path, method) => path.endsWith('/records/current') ? Response.json(status({ remote: { url: URL_PUSH, current_url: URL_PUSH, current_private: true, matches_last: true } })) : (path.endsWith('/records/commit') && method === 'POST' ? Response.json({ ok: true, commit: 'd'.repeat(40), pushed: true, url: URL_PUSH }) : undefined)
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Commit and push' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect((await within(dialog).findByTestId('records-export-warning')).textContent).toBe(`Push publishes every ticket in this workspace to ${URL_PUSH}, restricted ones included.`)
  expect(other('/records/commit')).toEqual([])
  expect(within(dialog).queryByTestId('records-private-line')).toBeNull()
  expect(dialog).toHaveTextContent(`orch trusts your word that only you read ${URL_PUSH}.`)
  await user.click(within(dialog).getByRole('button', { name: 'Commit and push' }))
  expect(await screen.findByTestId('records-result', {}, T)).toHaveTextContent('and pushed')
  expect(state.host.calls.find((c) => c.path.endsWith('/records/commit'))?.body).toEqual({ push: true })
})

it('records: a failed push shows the stable code and the commit that did land; a hidden message is withheld', async () => {
  const user = userEvent.setup()
  state.host.override = (path, method) =>
    path.endsWith('/records/commit') && method === 'POST'
      ? Response.json({ ok: false, exit: 3, error: { code: 'records.push_rejected', message: null, message_hidden: true, retryable: false, commit: 'e'.repeat(40) } }, { status: 409 })
      : undefined
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Save now' }, T))
  const failure = await screen.findByTestId('records-failure', {}, T)
  expect(failure).toHaveTextContent('records.push_rejected')
  expect(failure).toHaveTextContent(`A local commit already landed: ${'e'.repeat(40)}`)
  expect(failure).toHaveTextContent('cannot be shown here')
})

it('records: turning automatic commits off posts nothing at all (no body)', async () => {
  const user = userEvent.setup()
  state.host.override = (path, method) => (path.endsWith('/records/auto-off') && method === 'POST' ? Response.json({ ok: true }) : undefined)
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Turn automatic commits off' }, T))
  await screen.findByTestId('records-result', {}, T)
  const call = state.host.calls.find((c) => c.path.endsWith('/records/auto-off'))
  expect(call?.method).toBe('POST')
  expect(call?.body).toBeUndefined()
})

it('records: no "off" button when automatic commits are not on', async () => {
  state.host.workspaces[0].records = status({ auto: { on: false, every_min: 15, push: false } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await screen.findByRole('button', { name: 'Save now' }, T)
  expect(screen.queryByRole('button', { name: 'Turn automatic commits off' })).toBeNull()
})

it('records: configured false says there is no repository yet, offers Save now and still shows what is pending', async () => {
  state.host.workspaces[0].records = status({ configured: false, repo: null, last: null, auto: null, remote: null })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByTestId('records-not-configured', {}, T)).toHaveTextContent('No records repository yet; the first commit creates it.')
  expect(screen.getByTestId('records-not-configured')).not.toHaveTextContent('orch records init')
  expect(screen.getByText('12 events in 1 ticket, plus 5 workspace events')).toBeInTheDocument()
  expect(screen.getByText('nothing noted yet')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save now' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: 'Commit and push' })).toBeNull() // no remote to push to
})

it('records: configured true with last null keeps "nothing noted yet" and no not-configured line', async () => {
  state.host.workspaces[0].records = status({ configured: true, last: null })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByText('nothing noted yet', {}, T)).toBeInTheDocument()
  expect(screen.queryByTestId('records-not-configured')).toBeNull()
})

it('records: an absent cap or op is an honest "not served"', async () => {
  state.host.workspaces[0].records = { available: false }
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByTestId('records-unavailable', {}, T)).toHaveTextContent('does not serve records')
  expect(screen.queryByRole('button', { name: 'Save now' })).toBeNull()
})

it('records: members get no status request and no buttons (owners and maintainers only)', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByTestId('records-role', {}, T)).toHaveTextContent('owners and maintainers')
  expect(other('/records')).toEqual([])
})

it('records: a maintainer may run it; a host refusal stays a refusal', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  state.host.workspaces[0].records = undefined
  state.host.override = (path) => (path.endsWith('/records') ? Response.json({ ok: false, exit: 4, error: { code: 'role.denied', message: 'role.denied', retryable: false } }, { status: 403 }) : undefined)
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  expect(await screen.findByTestId('records-refused', {}, T)).toHaveTextContent('role.denied')
  expect(screen.queryByRole('button', { name: 'Save now' })).toBeNull()
})

it('without the records feature the tab is absent', async () => {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  show('/w/DEMO/settings/general')
  await screen.findByText('Advanced', {}, T)
  expect(screen.queryByRole('link', { name: 'Records' })).toBeNull()
})

// ---------------------------------------------------------------- the runner line on the workspace card

it('the card shows the D-P2-31 line verbatim whenever runner.enabled, whatever terminals says', async () => {
  for (const terminals of [true, false]) {
    state.host.workspaces[0].runner = { connected: false, enabled: true, terminals }
    queryClient.clear()
    const { unmount } = show('/w/DEMO/settings/general')
    expect((await screen.findByTestId('runner-execution-line', {}, T)).textContent).toBe(DASHBOARD_EXECUTION_LINE)
    unmount()
  }
})

it('runner disabled: no execution line, a button turns it on (no terminal command)', async () => {
  state.host.workspaces[0].runner = { connected: false, enabled: false, terminals: false }
  show('/w/DEMO/settings/general')
  const card = await screen.findByTestId('workspace-card-runner', {}, T)
  expect(screen.queryByTestId('runner-execution-line')).toBeNull()
  expect(card).not.toHaveTextContent('orch runner')
  expect(within(card).getByTestId('card-runner')).toHaveTextContent('Turn on dashboard execution')
})

it('a host that says nothing about the runner draws nothing (never a guess)', async () => {
  state.host.workspaces[0].runner = null
  show('/w/DEMO/settings/general')
  await screen.findByText('Advanced', {}, T)
  expect(screen.queryByTestId('workspace-card-runner')).toBeNull()
  expect(screen.queryByTestId('runner-execution-line')).toBeNull()
})



it('conversion does not prepare if the fresh ticket read fails', async () => {
  const user = userEvent.setup()
  state.host.override = (path) => path.endsWith('/tickets/DEMO-0014') ? Response.json({ error: { code: 'not_found', message: 'not found' } }, { status: 404 }) : undefined
  show('/w/DEMO/quick')
  await user.click(await screen.findByRole('button', { name: 'More for DEMO-0014' }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Make a normal ticket' }, T))
  expect(await screen.findByRole('alert', {}, T)).toHaveTextContent('Could not prepare conversion')
  expect(state.host.posts()).toEqual([])
})

it('records: hostile failure prose is isolated in a labelled tool-output block', async () => {
  const user = userEvent.setup()
  state.host.override = (path, method) => path.endsWith('/records/commit') && method === 'POST'
    ? Response.json({ ok: false, exit: 3, error: { code: 'records.push_rejected', message: '<a href="https://evil.example.test">run this</a>', retryable: false } }, { status: 409 }) : undefined
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Save now' }, T))
  const failure = await screen.findByTestId('records-failure', {}, T)
  const output = within(failure).getByLabelText('Output of the records tool')
  expect(output).toHaveTextContent('<a href="https://evil.example.test">run this</a>')
  expect(within(output).queryByRole('link')).toBeNull()
  expect(failure.querySelector('p')).toHaveTextContent('Not done.')
  const details = within(failure).getByText('Details').closest('details')!
  expect(details).not.toHaveAttribute('open')
  expect(details).toHaveTextContent('records.push_rejected')
  expect(failure.querySelector('p')).not.toHaveTextContent('run this')
})


it('quick alone becomes an empty label set', async () => {
  clickOn(state.host.workspaces[0])
  const user = userEvent.setup()
  state.host.override = (path) => path.endsWith('/tickets/DEMO-0010') ? Response.json({ ...ticket, key: 'DEMO-0010', labels: ['quick'] }) : undefined
  show('/w/DEMO/quick')
  await user.click(await screen.findByRole('button', { name: 'More for DEMO-0010' }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Make a normal ticket' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(dialog).toHaveTextContent('Labels kept: none')
  await prepareByClick(dialog, 'Make DEMO-0010 a normal ticket')
  expect(state.host.clickPrepares()[0].body).toMatchObject({ action: 'details', labels: [] })
})

it.each(['not-private', 'mismatch', 'failure', 'null', 'hidden'])('records current check: %s', async (kind) => {
  const user = userEvent.setup()
  const currentURL = 'https://git.example.test/current'
  let release!: () => void
  vi.spyOn(api, 'getRecordsCurrent').mockImplementationOnce(async () => {
    await new Promise<void>((resolve) => { release = resolve })
    if (kind === 'failure') throw new ApiError(409, { code: 'records.busy', message: 'busy', retryable: true })
    return { available: true, remote: { url: URL_PUSH, url_hidden: false, private: false, export: 'ok', current_url: kind === 'null' || kind === 'hidden' ? null : currentURL, current_url_hidden: kind === 'hidden', current_private: false, matches_last: kind !== 'mismatch' } }
  })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Commit and push' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(dialog).toHaveTextContent('Checking where push goes…')
  expect(within(dialog).getByRole('button', { name: 'Commit and push' })).toBeDisabled()
  await waitFor(() => expect(release).toBeTypeOf('function'))
  release()
  const warning = await within(dialog).findByTestId('records-export-warning')
  expect(dialog).not.toHaveTextContent('orch trusts your word')
  if (['failure', 'null', 'hidden'].includes(kind)) {
    expect(warning).toHaveTextContent(`last known: ${URL_PUSH}`)
    expect(dialog).toHaveTextContent("Couldn't check the current destination")
    expect(dialog).toHaveTextContent(kind === 'failure' ? 'records.busy' : 'records.current_url_unavailable')
  } else {
    expect(warning.textContent).toBe(`Push publishes every ticket in this workspace to ${currentURL}, restricted ones included.`)
    if (kind === 'mismatch') expect(dialog).toHaveTextContent(`This is not where the last push went (last: ${URL_PUSH}).`)
  }
})


it('records current check as a maintainer: host only, no private line (L2)', async () => {
  const user = userEvent.setup()
  state.host.workspaces[0].role = 'maintainer'
  state.host.workspaces[0].roster = roster().map((m) => (m.person === OWNER ? { ...m, role: 'maintainer' as const } : m))
  vi.spyOn(api, 'getRecordsCurrent').mockResolvedValueOnce({ available: true, remote: { url: 'git.example.test', url_hidden: false, private: true, export: 'ok', current_url: 'git.example.test', current_url_hidden: false, current_private: true, matches_last: true } } as never)
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await user.click(await screen.findByRole('button', { name: 'Commit and push' }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  const warning = await within(dialog).findByTestId('records-export-warning')
  expect(warning.textContent).toBe('Push publishes every ticket in this workspace to the remote configured in the records repository (on git.example.test; only owners see the full address), restricted ones included.')
  expect(within(dialog).queryByTestId('records-current-private')).toBeNull()
})

it.each(['ok', 'index_differs', 'unsafe'])('configured %s before the first commit has a repository', async (repoState) => {
  state.host.workspaces[0].records = status({ configured: true, last: null, repo: { state: repoState } })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
  await screen.findByText('nothing noted yet', {}, T)
  expect(screen.queryByTestId('records-not-configured')).toBeNull()
})

it.each([{ status: 'done', labels: ['quick'] }, { status: 'closed', labels: ['quick'] }, { status: 'open', labels: ['docs'] }])('fresh ticket refuses conversion: %j', async (fresh) => {
  const user = userEvent.setup()
  state.host.override = (path) => path.endsWith('/tickets/DEMO-0010') ? Response.json({ ...ticket, key: 'DEMO-0010', ...fresh }) : undefined
  show('/w/DEMO/quick')
  await user.click(await screen.findByRole('button', { name: 'More for DEMO-0010' }, T))
  await user.click(await screen.findByRole('menuitem', { name: 'Make a normal ticket' }, T))
  await screen.findByRole('alert', {}, T)
  expect(state.host.posts()).toEqual([])
})


const openRecords = async () => {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const advancedRecords = screen.queryByText('For advanced users')
  if (advancedRecords) await userEvent.setup().click(advancedRecords)
}
const CURRENT = { available: true, remote: { url: URL_PUSH, url_hidden: false, private: false, export: 'ok', current_url: URL_PUSH, current_url_hidden: false, current_private: false, matches_last: true } }

it('records C15: marking the remote private shows the exact address and one click sends exactly that address', async () => {
  const user = userEvent.setup()
  vi.spyOn(api, 'getRecordsCurrent').mockResolvedValue(CURRENT as never)
  const mark = vi.spyOn(api, 'recordsMark').mockResolvedValue({ ok: true } as never)
  await openRecords()
  await user.click(await screen.findByRole('button', { name: 'Mark the remote as private' }, T))
  const dialog = await screen.findByTestId('records-admin-dialog', {}, T)
  await within(dialog).findByText(URL_PUSH, {}, T)
  expect(dialog).toHaveTextContent('only you can read')
  expect(within(dialog).queryByRole('textbox')).toBeNull() // nothing is typed
  await user.click(within(dialog).getByRole('button', { name: 'Mark as private' }))
  await waitFor(() => expect(mark).toHaveBeenCalledWith(A, URL_PUSH))
})

it('records C15: an address that cannot be shown cannot be clicked through', async () => {
  const user = userEvent.setup()
  vi.spyOn(api, 'getRecordsCurrent').mockResolvedValue({ available: true, remote: { ...CURRENT.remote, current_url: null, current_url_hidden: true } } as never)
  const first = vi.spyOn(api, 'recordsPushInitial').mockResolvedValue({ ok: true } as never)
  await openRecords()
  await user.click(await screen.findByRole('button', { name: 'Create the remote branch' }, T))
  const dialog = await screen.findByTestId('records-admin-dialog', {}, T)
  await within(dialog).findByRole('alert', {}, T)
  expect(within(dialog).getByRole('button', { name: 'Create branch and upload tickets' })).toBeDisabled()
  expect(first).not.toHaveBeenCalled()
})

it('records C15: the first push says it uploads restricted tickets and sends the shown address', async () => {
  const user = userEvent.setup()
  vi.spyOn(api, 'getRecordsCurrent').mockResolvedValue(CURRENT as never)
  const first = vi.spyOn(api, 'recordsPushInitial').mockResolvedValue({ ok: true, commit: 'd'.repeat(40), pushed: true, url: URL_PUSH } as never)
  await openRecords()
  await user.click(await screen.findByRole('button', { name: 'Create the remote branch' }, T))
  const dialog = await screen.findByTestId('records-admin-dialog', {}, T)
  await within(dialog).findByText(URL_PUSH, {}, T)
  expect(dialog).toHaveTextContent('Restricted tickets are included')
  await user.click(within(dialog).getByRole('button', { name: 'Create branch and upload tickets' }))
  await waitFor(() => expect(first).toHaveBeenCalledWith(A, URL_PUSH))
  expect(await screen.findByTestId('records-result', {}, T)).toHaveTextContent('pushed')
})

it('records C15: Set up runs init, and a private remote offers Remove the private mark', async () => {
  const user = userEvent.setup()
  state.host.workspaces[0].records = status({ configured: false, repo: null, remote: { url: URL_PUSH, url_hidden: false, private: true, export: 'ok' } })
  const init = vi.spyOn(api, 'recordsInit').mockResolvedValue({ ok: true } as never)
  const mark = vi.spyOn(api, 'recordsMark').mockResolvedValue({ ok: true } as never)
  await openRecords()
  await user.click(await screen.findByRole('button', { name: 'Set up' }, T))
  await waitFor(() => expect(init).toHaveBeenCalledWith(A))
  await user.click(await screen.findByRole('button', { name: 'Remove the private mark' }, T))
  const dialog = await screen.findByTestId('records-admin-dialog', {}, T)
  await user.click(within(dialog).getByRole('button', { name: 'Remove private label' }))
  await waitFor(() => expect(mark).toHaveBeenCalledWith(A, null))
})
