import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { DO, GRANT_A, GRANT_B, MARA, OWNER, READS, TOM, grant, policy, settingsOf, useFakeHost, workspace, show } from '@/test/hostHarness'

// Every page of the app reads through the production api, as the build wires it (vite.config.ts alias).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: ID })])

it('the Settings link is offered when the host serves it, with only the reduced sections', async () => {
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('heading', { name: 'General' }, T)).toBeInTheDocument()
  const nav = within(screen.getByRole('navigation', { name: 'Settings' }))
  expect(nav.getAllByRole('link').map((a) => a.textContent)).toEqual(['General', 'Members', 'Gates', 'Grants', 'Add-ons', 'Devices'])
  expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  expect(screen.queryByText('Relay & devices')).not.toBeInTheDocument()
})

it('Devices on a real host: pairing a phone is not available yet, and no pairing request is made', async () => {
  show('/w/DEMO/settings/devices')
  expect(await screen.findByTestId('pair-unavailable', {}, T)).toHaveTextContent('Pairing a phone comes later. For now you approve on this computer, in the terminal.')
  expect(state.host.calls.filter((c) => /pair|device/.test(c.path))).toEqual([])
  expect(state.host.posts()).toEqual([])
  expect(screen.queryByRole('button', { name: 'Create pairing link' })).toBeNull()
})

it('General shows what the host says, marks defaults and lists repositories with what the host sees on disk', async () => {
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('button', { name: '8 h' }, T)).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByLabelText(/Ticket reserved for/)).toHaveValue(120)
  expect(screen.getAllByText(/The default\./).length).toBe(2) // claim and lease; the grant length is not a default here
  const repos = screen.getByRole('list', { name: 'Repositories' })
  expect(within(repos).getByText('https://git.example.test/a/code')).toBeInTheDocument()
  expect(within(repos).getByText('/srv/code')).toBeInTheDocument()
  expect(screen.getByTestId('repo-check-code')).toHaveTextContent('In place, on branch main')
  expect(screen.getByRole('button', { name: 'Add repository' })).toBeInTheDocument() // an owner on a host that serves settings.set
})

it('Repositories: adding one validates in the form, then hands the typed action to the signer', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await user.click(await screen.findByRole('button', { name: 'Add repository' }, T))
  const form = await screen.findByRole('dialog', {}, T)
  await user.type(within(form).getByLabelText('Name'), 'api')
  await user.type(within(form).getByLabelText('Folder on this computer'), 'relative/dir')
  await user.type(within(form).getByLabelText(/Remote/), 'https://user:tok@git.example.test/a')
  await user.click(within(form).getByRole('button', { name: 'Review and sign' }))
  expect(within(form).getAllByRole('alert').length).toBe(2) // the folder and the remote; nothing was sent
  expect(state.host.posts()).toEqual([])
  await user.clear(within(form).getByLabelText('Folder on this computer'))
  await user.type(within(form).getByLabelText('Folder on this computer'), '/work/api')
  await user.clear(within(form).getByLabelText(/Remote/))
  await user.click(within(form).getByRole('button', { name: 'Review and sign' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'repo_set', name: 'api', path: '/work/api' }])
})

it('Repositories: removing one hands off repo_remove, and a name already listed is refused in the form', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await user.click(await screen.findByRole('button', { name: 'Add repository' }, T))
  const form = await screen.findByRole('dialog', {}, T)
  await user.type(within(form).getByLabelText('Name'), 'code')
  await user.type(within(form).getByLabelText('Folder on this computer'), '/x')
  await user.click(within(form).getByRole('button', { name: 'Review and sign' }))
  expect(within(form).getByRole('alert')).toHaveTextContent('already in the list')
  await user.click(within(form).getByRole('button', { name: 'Cancel' }))
  await user.click(screen.getByRole('button', { name: 'Remove code' }))
  await within(await screen.findByRole('dialog', {}, T)).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'repo_remove', name: 'code' }])
})

it('Repositories: removing a repo that tickets link warns with their keys before anything is signed', async () => {
  const user = userEvent.setup()
  state.host.workspaces[0].settings = settingsOf({ repos: [{ name: 'code', default_branch: 'main', remote: null, tickets: ['DEMO-0001', 'DEMO-0004'] }] })
  show('/w/DEMO/settings/general')
  await user.click(await screen.findByRole('button', { name: 'Remove code' }, T))
  const warn = await screen.findByRole('dialog', {}, T)
  expect(warn).toHaveTextContent('DEMO-0001, DEMO-0004')
  expect(state.host.posts()).toEqual([])
  await user.click(within(warn).getByRole('button', { name: 'Keep it' }))
  expect(state.host.posts()).toEqual([])
  await user.click(screen.getByRole('button', { name: 'Remove code' }))
  await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: /Continue to review and sign/ }))
  await waitFor(() => expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'repo_remove', name: 'code' }]), T)
})

it('Repositories: a member sees the list and no way to change it', async () => {
  state.host.workspaces[0].role = 'member'
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].settings = settingsOf({ repos: [{ name: 'code', default_branch: 'main', remote: null }] })
  show('/w/DEMO/settings/general')
  expect(await screen.findByRole('list', { name: 'Repositories' }, T)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add repository' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Remove code' })).toBeNull()
  expect(screen.queryByTestId('repo-check-code')).toBeNull()
  expect(screen.getByText('Only owners can add, change or remove repositories.')).toBeInTheDocument()
})

it('a host without the settings cap says so and shows no value', async () => {
  state.host.workspaces[0].settings = settingsOf({ available: false, grant_hours: null, claim_ttl_min: null, lease_ttl_min: null, defaults: null, gates: null, repos: null })
  show('/w/DEMO/settings/general')
  expect(await screen.findByTestId('settings-unavailable', {}, T)).toHaveTextContent('does not say what the settings are')
  expect(screen.queryByLabelText(/Agent grant length/)).not.toBeInTheDocument()
  expect(screen.getByText('This host does not say which repositories there are.')).toBeInTheDocument()
})

it('changing the grant length opens the hand-off with the typed action, and shows the host material, not the form', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const input = await screen.findByRole('button', { name: '7 days' }, T)
  await user.click(input)
  await user.click(screen.getByRole('button', { name: 'Save settings' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'settings_set', grant_hours: 168 }])
  expect(within(dialog).getByTestId('handoff-command')).toHaveTextContent('orch sign 7F3K-Q2MA')
  expect(dialog).toHaveTextContent('Change to the workspace settings')
  expect(dialog).toHaveTextContent('In your terminal you type DEMO~aaaaaaaa/SETTINGS to continue.')
  expect(dialog).toHaveTextContent('HOST-BEFORE-VALUE') // the host's "current value, not signed", labelled as such
  expect(dialog).toHaveTextContent('Current values according to the host, not signed')
  expect(within(dialog).queryByLabelText(/passphrase/i)).toBeNull()
  // The three durations are validated before any request exists.
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(state.host.deletes().length).toBe(1))
})

it('out-of-range durations never reach the host', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const input = await screen.findByLabelText(/Ticket reserved for/, {}, T)
  await user.clear(input)
  await user.type(input, '999999')
  expect(screen.getByRole('alert')).toHaveTextContent('whole number')
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  expect(state.host.posts()).toEqual([])
})

it('a member sees the settings and cannot save them', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/general')
  const input = await screen.findByRole('button', { name: '7 days' }, T)
  expect(input).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  expect(screen.getByText('Only owners can save.')).toBeInTheDocument()
})

it('a host that does not list settings.set cannot change settings: the control says why', async () => {
  state.host.workspaces[0].features = READS
  show('/w/DEMO/settings/general')
  const input = await screen.findByRole('button', { name: '7 days' }, T)
  expect(input).toBeDisabled()
  expect(screen.getByText('This host cannot change settings yet.')).toBeInTheDocument()
})

it('Members lists the roster with devices and no last-seen, and a role change is a hand-off', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  expect(await screen.findByRole('heading', { name: /Members/ }, T)).toBeInTheDocument()
  const table = await screen.findByRole('table', {}, T)
  expect(within(table).queryByText('Last seen')).not.toBeInTheDocument()
  expect(within(table).getByText('Mara')).toBeInTheDocument()
  expect(screen.getByRole('list', { name: 'Devices of Mara' })).toHaveTextContent('look, decide, operate, type')
  await user.selectOptions(screen.getByLabelText('Role of Tom'), 'maintainer')
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(dialog).toHaveTextContent('Make Tom a maintainer')
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'member_role', person: TOM, role: 'maintainer' }])
  expect(dialog).toHaveTextContent('Change of a member')
  expect(dialog).toHaveTextContent(`In your terminal you type ${TOM} to continue.`)
})

it('a member whose host name has invisible characters shows by id with a note, and can still be removed', async () => {
  // Opus RH review L1: one such name must not take the workspace or the roster down (the server sends the person id).
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => (m.person === TOM ? { ...m, name: TOM, name_hidden: true } : m))
  show('/w/DEMO/settings/members')
  const table = await screen.findByRole('table', {}, T)
  expect(within(table).getByText('Name not shown: it has invisible characters')).toBeInTheDocument()
  expect(within(table).getByText('Mara')).toBeInTheDocument()
  expect(screen.getByLabelText(`Remove ${TOM}`)).toBeInTheDocument()
})

it('removing a member is a hand-off; you cannot remove yourself or the last owner', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('table', {}, T)
  expect(screen.getByRole('button', { name: 'Remove Sev' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Remove Tom' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'member_remove', person: TOM }])
})

it('a maintainer adds members and viewers, removes the same, and changes no roles', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('table', {}, T)
  expect(screen.getByLabelText('Role of Tom')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Remove Sev' })).not.toBeInTheDocument() // an owner
  expect(screen.getByRole('button', { name: 'Remove Tom' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: /Add member/ }))
  const form = await screen.findByRole('dialog')
  expect(within(form).getAllByRole('option').map((o) => o.textContent)).toEqual(['maintainer', 'member', 'viewer'].filter((r) => r !== 'maintainer'))
})

const PK = 'B' + 'x'.repeat(86)
const CERT = JSON.stringify({ o: { device_id: '4'.repeat(32) }, sig: 'demo-not-a-real-signature' })

it('adding a member takes the invitee\'s own files, checks them, and the host shows what the certificate says', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('table', {}, T)
  await user.click(screen.getByRole('button', { name: /Add member/ }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Name'), 'Nia')
  await user.upload(within(form).getByLabelText('Public key file'), new File([PK + '\n'], 'nia.pk', { type: 'text/plain' }))
  await user.upload(within(form).getByLabelText('Device certificate file'), new File([CERT], 'nia.cert.json', { type: 'application/json' }))
  expect(within(form).getByLabelText('Public key')).toHaveValue(PK + '\n')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  const dialog = await screen.findByRole('dialog', { name: /Add Nia/ }, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'member_add', name: 'Nia', role: 'member', pk: PK, cert: CERT }])
  // What the host read in the certificate is what the person is shown, with the person id to compare out of band.
  expect(dialog).toHaveTextContent('Add a member')
  expect(dialog).toHaveTextContent('p_' + 'd'.repeat(32))
  expect(dialog).toHaveTextContent('d_' + '4'.repeat(32))
  expect(dialog).toHaveTextContent('operate')
  expect(dialog).toHaveTextContent(`In your terminal you type p_${'d'.repeat(32)} to continue.`)
})

it('the member form refuses a key or certificate that cannot be one, before any request', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('table', {}, T)
  await user.click(screen.getByRole('button', { name: /Add member/ }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Name'), 'Nia')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['Choose or paste their public key.', 'Choose or paste their device certificate.'])
  await user.type(within(form).getByLabelText('Public key'), 'short')
  await user.type(within(form).getByLabelText('Device certificate'), '[[1')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['A public key is 87 characters of base64url (65 bytes).', 'The certificate file is not JSON.'])
  // A file larger than the field takes is refused before it is read.
  await user.upload(within(form).getByLabelText('Device certificate file'), new File(['x'.repeat(16385)], 'big.json', { type: 'application/json' }))
  expect(within(form).getByText(/big.json is larger than 16384 bytes/)).toBeInTheDocument()
  expect(state.host.posts()).toEqual([])
})

it('Gates: the code review is never approved by the assignees and always independent; the policy goes out in full', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  expect(await screen.findByRole('heading', { name: 'Gate policies' }, T)).toBeInTheDocument()
  expect(screen.getByText(/ends the approvals already given at that gate/)).toBeInTheDocument()
  const matrix = screen.getByRole('table', { name: 'Code review: who may approve' })
  expect(within(matrix).getByRole('checkbox', { name: 'The assignees may not approve' })).toBeChecked()
  expect(within(matrix).getByRole('checkbox', { name: 'The assignees may not approve' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: /Code review: approvers independent.*always/ })).toBeDisabled()
  await user.click(screen.getByRole('radio', { name: 'Every ticket' }))
  await user.click(screen.getByRole('button', { name: 'Change code review policy' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  await within(dialog).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([
    { action: 'policy_set', gate: 'code', policy: { approvers: ['maintainer', 'owner'], count: 1, not: ['assignees'], applies: 'all', independent: true } },
  ])
  expect(dialog).toHaveTextContent('Change to a gate policy')
})

it('Gates: another gate changes approvers and count; a policy with no approver is not saved', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Gate policies' }, T)
  const who = screen.getByRole('table', { name: 'Plan: who may approve' })
  await user.click(within(who).getByRole('checkbox', { name: 'Owners may approve' }))
  expect(screen.getAllByRole('alert')[0]).toHaveTextContent('Pick at least one kind of approver.')
  expect(screen.getByRole('button', { name: 'Change plan policy' })).toBeDisabled()
  await user.click(within(who).getByRole('checkbox', { name: 'Maintainers may approve' }))
  await user.click(within(who).getByRole('checkbox', { name: 'Owners may approve' }))
  const count = screen.getByLabelText('Plan: approvals needed')
  await user.clear(count)
  await user.type(count, '2')
  await user.click(screen.getByRole('button', { name: 'Change plan policy' }))
  await screen.findByRole('dialog', {}, T)
  await waitFor(() => expect(state.host.posts().length).toBe(1))
  expect(state.host.posts()[0].body).toMatchObject({ action: 'policy_set', gate: 'plan', policy: { approvers: ['maintainer', 'owner'], count: 2, not: [], applies: 'all', independent: false } })
})

it('Gates: a non-owner reads the policies and cannot change them', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Gate policies' }, T)
  expect(screen.getByRole('button', { name: 'Change plan policy' })).toBeDisabled()
  expect(screen.getAllByText('Only owners can change this gate.').length).toBe(4)
})

it('Grants: an owner revokes any active grant by hand-off, with an optional reason', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/grants')
  expect(await screen.findByRole('heading', { name: 'Agent grants' }, T)).toBeInTheDocument()
  expect(await screen.findByText(GRANT_A, {}, T)).toBeInTheDocument()
  expect(screen.getByText(GRANT_B)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Revoke the grant of Tom' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Revoke the grant of Tom' }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Why (optional)'), 'no longer needed')
  await user.click(within(form).getByRole('button', { name: 'Revoke grant' }))
  await within(await screen.findByRole('dialog')).findByTestId('handoff-command', {}, T)
  expect(state.host.posts().map((c) => c.body)).toEqual([{ action: 'grant_revoke', grant: GRANT_B, reason: 'no longer needed' }])
  expect(screen.getByRole('dialog')).toHaveTextContent('In your terminal you type GRANT.REVOKE to continue.')
})

it('Grants: a member gets the list the host gives them and revokes only their own', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  state.host.workspaces[0].grants = [grant(GRANT_B, TOM, { scope: 'workable' })] // the host sends a member their own grants only
  show('/w/DEMO/settings/grants')
  expect(await screen.findByText(GRANT_B, {}, T)).toBeInTheDocument()
  expect(screen.queryByText(GRANT_A)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Revoke the grant of Tom' })).toBeEnabled()
  expect(screen.getByText(/Owners see and revoke every grant/)).toBeInTheDocument()
})

it('Grants: ended grants are shown on request, with who revoked them only when the host says', async () => {
  const user = userEvent.setup()
  state.host.workspaces[0].grants = [
    grant(GRANT_A, OWNER),
    grant(GRANT_B, TOM, { revoked: { at: '2026-10-12T09:00:00Z', by: MARA, seq: 7 } }),
    grant('gr_' + '0'.repeat(25) + 'C', TOM, { revoked: { at: '2026-10-12T09:30:00Z', by: null, seq: null }, session_ids: null }),
  ]
  show('/w/DEMO/settings/grants')
  await screen.findByText(GRANT_A, {}, T)
  expect(screen.queryByText(GRANT_B)).not.toBeInTheDocument()
  await user.click(screen.getByRole('checkbox', { name: 'Show ended grants' }))
  await screen.findByText(GRANT_B, {}, T)
  expect(screen.getByText(GRANT_B).closest('tr')).toHaveTextContent(/by\s*Mara/)
  // No `read_ext/2`: who revoked it is unknown, and nothing is made up.
  const unknown = screen.getByText('gr_' + '0'.repeat(25) + 'C').closest('tr')!
  expect(unknown).toHaveTextContent('Revoked')
  expect(unknown).not.toHaveTextContent(/ by /)
  expect(state.host.calls.some((c) => c.path.endsWith('grants?active=false'))).toBe(true)
  expect(within(unknown).queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument()
})

it('Grants: the runner grant is marked from grant.list label (H11c-2); another label is plain text', async () => {
  state.host.workspaces[0].grants = [grant(GRANT_A, OWNER, { label: 'runner' }), grant(GRANT_B, TOM, { label: 'tests' })]
  show('/w/DEMO/settings/grants')
  const runner = (await screen.findByText(GRANT_A, {}, T)).closest('tr')!
  expect(within(runner).getByTestId('runner-grant-tag')).toHaveTextContent('runner grant')
  const other = screen.getByText(GRANT_B).closest('tr')!
  expect(within(other).queryByTestId('runner-grant-tag')).not.toBeInTheDocument()
  expect(other).toHaveTextContent('label tests')
})

it('host strings with bidi characters are shown escaped, not applied', async () => {
  state.host.workspaces[0].grants = [grant(GRANT_A, OWNER, { scope: 'al\u202el' })]
  show('/w/DEMO/settings/grants')
  const row = (await screen.findByText(GRANT_A, {}, T)).closest('tr')!
  expect(row.textContent).toContain('\\u{202e}')
  expect(row.textContent).not.toContain('\u202e')
})

it('settings, policy and grant live items refresh the settings queries', async () => {
  const { invalidateLive } = await vi.importActual<typeof import('@/api/useLiveUpdates')>('@/api/useLiveUpdates')
  const { queryClient } = await import('@/production/Shell')
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Gate policies' }, T)
  const spy = vi.spyOn(queryClient, 'invalidateQueries')
  for (const type of ['settings.changed', 'policy.changed', 'grant.revoked']) invalidateLive(queryClient, ID, 'items', [{ id: '9.e', type }])
  const keys = spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown[] }).queryKey))
  expect(keys).toEqual(expect.arrayContaining([`["settings","${ID}"]`, `["settings-grants","${ID}"]`, `["members","${ID}"]`]))
})

it('a viewer role reads the settings and signs nothing', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'viewer'
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => (m.person === TOM ? { ...m, role: 'viewer' as const } : m))
  state.host.workspaces[0].features = [...READS, ...DO]
  show('/w/DEMO/settings/members')
  await screen.findByRole('table', {}, T)
  expect(screen.queryByRole('button', { name: /Add member/ })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Role of Mara')).toBeDisabled()
  expect(state.host.calls.filter((c) => c.method !== 'GET')).toEqual([])
})

void policy

it('Gates: a one-person workspace explains that the verify rule does not block its only member', async () => {
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.slice(0, 1)
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Gate policies' }, T)
  expect(await screen.findByTestId('gate-alone', {}, T)).toHaveTextContent('only member')
})

it('How I approve: the production api asks the server, so the personal controls appear when it answers (B11 10)', async () => {
  state.host.workspaces[0].settings = settingsOf({ sign_methods: ['terminal', 'click'] })
  state.host.override = (path) =>
    path.endsWith('/me/sign_default') ? Response.json({ method: 'terminal', is_default: true, allowed: ['terminal', 'click'] }) : undefined
  show('/w/DEMO/settings/general')
  expect(await screen.findByLabelText('Default', {}, T)).toBeEnabled()
  expect(state.host.calls.some((c) => c.path.endsWith('/me/sign_default'))).toBe(true)
})

it('How I approve: an older server without the route hides the personal controls and says so in one line', async () => {
  state.host.workspaces[0].settings = settingsOf({ sign_methods: ['terminal', 'click'] })
  show('/w/DEMO/settings/general')
  expect(await screen.findByText(/Your own way of approving is not available on this host yet/, {}, T)).toBeInTheDocument()
  expect(screen.queryByLabelText('Default')).toBeNull()
})
