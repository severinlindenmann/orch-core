import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { agentsApi } from '@/api/agents'
import { ARM_MS } from '@/api/click'
import { READERS_SENTENCE } from '@/api/refusals'
import { DO, GRANT_A, GRANT_B, MARA, OWNER, READS, TOM, clickOn as clickOnFor, grant, policy, prepareByClick, settingsOf, trustedPresses, useFakeHost, workspace, show } from '@/test/hostHarness'

// Every page of the app reads through the production api, as the build wires it (vite.config.ts alias).
vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const ID = 'a'.repeat(32)
const T = { timeout: 5000 }
const state = useFakeHost(() => [workspace({ id: ID })])
afterEach(() => vi.restoreAllMocks())

it('the Settings link is offered when the host serves it, with only the reduced sections', async () => {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  expect(await screen.findByText('Advanced', {}, T)).toBeInTheDocument()
  const nav = within(screen.getByRole('navigation', { name: 'Settings' }))
  expect(nav.getAllByRole('link').map((a) => a.textContent)).toEqual(['General', 'Members', 'Approvals', 'Agent access', 'Devices'])
  expect(screen.getByRole('link', { name: 'Settings' })).toBeInTheDocument()
  expect(screen.queryByText('Relay & devices')).not.toBeInTheDocument()
})

it('Devices have one home, Settings > Devices (the Phone block moved from Add-ons): pairing is not available yet, and no pairing request is made', async () => {
  show('/w/DEMO/settings/devices')
  expect(await screen.findByTestId('addon-phone', {}, T)).toHaveTextContent('Phone')
  expect(await screen.findByTestId('pair-unavailable', {}, T)).toHaveTextContent('Pairing a phone comes later. For now you approve on this computer, in the terminal.')
  expect(state.host.calls.filter((c) => /pair|device/.test(c.path))).toEqual([])
  expect(state.host.posts()).toEqual([])
  expect(screen.queryByRole('button', { name: 'Create pairing link' })).toBeNull()
})

it('the old /settings/addons URL goes to the Add-ons page', async () => {
  show('/w/DEMO/settings/addons')
  expect(await screen.findByRole('heading', { name: 'Built in' }, T)).toBeInTheDocument()
})

it('General shows what the host says, marks defaults and lists repositories with what the host sees on disk', async () => {
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  expect(await screen.findByRole('combobox', { name: 'Longest agent access you can give' }, T)).toHaveValue('8')
  expect(screen.getByLabelText(/Ticket reserved for/)).toHaveValue(120)
  expect(screen.getAllByText(/The default\./).length).toBe(2) // claim and lease; the grant length is not a default here
  const repos = screen.getByRole('list', { name: 'Repositories' })
  expect(within(repos).getByText('https://git.example.test/a/code')).toBeInTheDocument()
  expect(within(repos).getByText('/srv/code')).toBeInTheDocument()
  expect(screen.getByTestId('repo-check-code')).toHaveTextContent('In place, on branch main')
  expect(screen.getByRole('button', { name: 'Add folder' })).toBeInTheDocument() // an owner on a host that serves settings.set
})

it('Repositories: adding one validates in the form, then hands the typed action to the signer', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  await user.click(await screen.findByRole('button', { name: 'Add folder' }, T))
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
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Add repository api')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'repo_set', name: 'api', path: '/work/api' }])
  expect(state.host.posts()).toEqual([])
})

it('Repositories: removing one signs repo_remove, and a name already listed is refused in the form', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  await user.click(await screen.findByRole('button', { name: 'Add folder' }, T))
  const form = await screen.findByRole('dialog', {}, T)
  await user.type(within(form).getByLabelText('Name'), 'code')
  await user.type(within(form).getByLabelText('Folder on this computer'), '/x')
  await user.click(within(form).getByRole('button', { name: 'Review and sign' }))
  expect(within(form).getByRole('alert')).toHaveTextContent('already in the list')
  await user.click(within(form).getByRole('button', { name: 'Cancel' }))
  await user.click(screen.getByRole('button', { name: 'Remove code' }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Remove repository code')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'repo_remove', name: 'code' }])
})

it('Repositories: the host\'s refusal of a folder stays in the form, before any signing step', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  await user.click(await screen.findByRole('button', { name: 'Add folder' }, T))
  const form = await screen.findByRole('dialog', {}, T)
  await user.type(within(form).getByLabelText('Name'), 'api')
  await user.type(within(form).getByLabelText('Folder on this computer'), '/refused/api')
  await user.click(within(form).getByRole('button', { name: 'Review and sign' }))
  expect(await within(form).findByRole('alert', {}, T)).toHaveTextContent('This folder is not a git repository.')
  expect(state.host.posts()).toEqual([]) // nothing was prepared
  expect(screen.getByRole('dialog')).toBe(form)
})

it('Repositories: removing a repo that tickets link warns with their keys before anything is signed', async () => {
  clickOn()
  const user = userEvent.setup()
  state.host.workspaces[0].settings = settingsOf({ repos: [{ name: 'code', default_branch: 'main', remote: null, tickets: ['DEMO-0001', 'DEMO-0004'] }] })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  await user.click(await screen.findByRole('button', { name: 'Remove code' }, T))
  const warn = await screen.findByRole('dialog', {}, T)
  expect(warn).toHaveTextContent('DEMO-0001, DEMO-0004')
  expect(state.host.clickPrepares()).toEqual([])
  await user.click(within(warn).getByRole('button', { name: 'Keep it' }))
  expect(state.host.clickPrepares()).toEqual([])
  await user.click(screen.getByRole('button', { name: 'Remove code' }))
  await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: /Continue to review and sign/ }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Remove repository code')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'repo_remove', name: 'code' }])
})

it('Repositories: a member sees the list and no way to change it', async () => {
  state.host.workspaces[0].role = 'member'
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].settings = settingsOf({ repos: [{ name: 'code', default_branch: 'main', remote: null }] })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  expect(await screen.findByRole('list', { name: 'Repositories' }, T)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add folder' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Remove code' })).toBeNull()
  expect(screen.queryByTestId('repo-check-code')).toBeNull()
  expect(screen.getByText('Only owners can add, change or remove repositories.')).toBeInTheDocument()
})

it('a host without the settings cap says so and shows no value', async () => {
  state.host.workspaces[0].settings = settingsOf({ available: false, grant_hours: null, claim_ttl_min: null, lease_ttl_min: null, defaults: null, gates: null, repos: null })
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  expect(await screen.findByTestId('settings-unavailable', {}, T)).toHaveTextContent('does not say what the settings are')
  expect(screen.queryByLabelText(/Agent grant length/)).not.toBeInTheDocument()
  expect(screen.getByText('This host does not say which repositories there are.')).toBeInTheDocument()
})

it('changing the grant length signs the typed action with One click and shows the signer\'s View, not the form', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const input = await screen.findByRole('combobox', { name: 'Longest agent access you can give' }, T)
  await user.selectOptions(input, '168')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(state.host.clickPrepares()).toEqual([]) // nothing before the outcome button
  const panel = await prepareByClick(dialog, 'Change the workspace settings')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'settings_set', grant_hours: 168 }])
  expect(panel).toHaveTextContent('FROM-THE-SIGNER settings.set')
  expect(dialog).not.toHaveTextContent(/orch sign|In your terminal|HOST-BEFORE-VALUE/)
  expect(within(dialog).queryByRole('textbox')).toBeNull() // nothing is typed
  expect(state.host.posts()).toEqual([])
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
  await waitFor(() => expect(state.host.clickCancelled).toHaveLength(1), T)
  expect(state.host.clickSigned).toEqual([])
})

it('out-of-range durations never reach the host', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const input = await screen.findByLabelText(/Ticket reserved for/, {}, T)
  await user.clear(input)
  await user.type(input, '999999')
  expect(screen.getByText(/use a whole number/)).toHaveAttribute('role', 'alert')
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  expect(state.host.posts()).toEqual([])
})

it('a member sees the settings and cannot save them', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const input = await screen.findByRole('combobox', { name: 'Longest agent access you can give' }, T)
  expect(input).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  expect(screen.getAllByText('Only owners can save.')[0]).toBeInTheDocument()
})

it('a host that does not list settings.set cannot change settings: the control says why', async () => {
  state.host.workspaces[0].features = READS
  show('/w/DEMO/settings/general')
  await userEvent.setup().click(await screen.findByText('Advanced', {}, T))
  const input = await screen.findByRole('combobox', { name: 'Longest agent access you can give' }, T)
  expect(input).toBeDisabled()
  expect(screen.getAllByText('This host cannot change settings yet.')[0]).toBeInTheDocument()
})

it('Members lists the people with what they can do and no last-seen, and a role change is signed with One click', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  expect(await screen.findByRole('heading', { name: 'People' }, T)).toBeInTheDocument()
  const list = await screen.findByRole('list', { name: 'People' }, T)
  expect(within(list).queryByText('Last seen')).not.toBeInTheDocument()
  expect(within(list).getByText('Mara')).toBeInTheDocument()
  // The four roles in plain words, then what each person's devices may do (the four scopes, not their raw names).
  expect(screen.getByTestId('role-strip')).toHaveTextContent('Can read, cannot change')
  expect(screen.getByRole('list', { name: 'What Mara can do' })).toHaveTextContent('Can readCan approveCan change settingsCan use terminals')
  expect(within(list).queryByText(/operate/)).not.toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Role of Tom'), 'maintainer')
  const dialog = await screen.findByRole('dialog', {}, T)
  expect(dialog).toHaveTextContent('Make Tom a maintainer')
  const panel = await prepareByClick(dialog, 'Make Tom a maintainer')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'member_role', person: TOM, role: 'maintainer' }])
  expect(panel).toHaveTextContent('FROM-THE-SIGNER member.role')
  expect(state.host.posts()).toEqual([])
})

it('a device below the required level gets the "needs an update" chip; the signing device and a workspace without a requirement never do', async () => {
  state.host.workspaces[0].features = [...state.host.workspaces[0].features, 'readers']
  // Tom's device reads at level 0; everyone else's at level 1.
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => ({ ...m, devices: m.devices.map((d) => ({ ...d, reader: m.person === TOM ? 0 : 1 })) }))
  state.host.override = (path) => (path.endsWith('/readers') ? Response.json({ ok: true, available: true, required_reader: 1, readers: [] }) : undefined)
  show('/w/DEMO/settings/members')
  const list = await screen.findByRole('list', { name: 'People' }, T)
  await waitFor(() => expect(within(list).getAllByTestId('device-needs-update')).toHaveLength(1), T)
  const chip = within(list).getByTestId('device-needs-update')
  expect(chip).toHaveTextContent('Device update needed')
  expect(chip.closest('[data-testid="person-card"]')).toHaveTextContent('Tom')
  const card = chip.closest('[data-testid="person-card"]')!
  expect(card.querySelector('details')).not.toHaveAttribute('open')
  await userEvent.setup().click(chip) // opens that person's details
  expect(card.querySelector('details')).toHaveAttribute('open')
})

it('a person whose device is below the level is never flagged while that is the computer signing with one click', async () => {
  state.host.workspaces[0].features = [...state.host.workspaces[0].features, 'readers', 'click']
  state.host.workspaces[0].click = { custody: 'convenience', enabled: true, available: true }
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => ({ ...m, devices: m.devices.map((d) => ({ ...d, reader: m.person === OWNER ? 0 : 1 })) }))
  state.host.override = (path) => (path.endsWith('/readers') ? Response.json({ ok: true, available: true, required_reader: 1, readers: [] }) : undefined)
  show('/w/DEMO/settings/members')
  const list = await screen.findByRole('list', { name: 'People' }, T)
  expect(await within(list).findByText('This computer', {}, T)).toBeInTheDocument()
  await new Promise((r) => setTimeout(r, 200))
  expect(within(list).queryAllByTestId('device-needs-update').map((c) => c.closest('[data-testid="person-card"]')?.textContent)).toEqual([])
})

it('a member whose host name has invisible characters shows by id with a note, and can still be removed', async () => {
  // Opus RH review L1: one such name must not take the workspace or the roster down (the server sends the person id).
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.map((m) => (m.person === TOM ? { ...m, name: TOM, name_hidden: true } : m))
  show('/w/DEMO/settings/members')
  const list = await screen.findByRole('list', { name: 'People' }, T)
  expect(within(list).getByText('Name not shown: it has invisible characters')).toBeInTheDocument()
  expect(within(list).getByText('Mara')).toBeInTheDocument()
  expect(screen.getByLabelText(`Remove ${TOM}`)).toBeInTheDocument()
})

it('removing a member is signed with One click; you cannot remove yourself or the last owner', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('list', { name: 'People' }, T)
  expect(screen.getByRole('button', { name: 'Remove Sev' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Remove Tom' }))
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Remove Tom')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'member_remove', person: TOM }])
})

it('a maintainer adds members and viewers, removes the same, and changes no roles', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('list', { name: 'People' }, T)
  expect(screen.getByLabelText('Role of Tom')).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Remove Sev' })).not.toBeInTheDocument() // an owner
  expect(screen.getByRole('button', { name: 'Remove Tom' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: /Add person/ }))
  const form = await screen.findByRole('dialog')
  expect(within(form).getAllByRole('option').map((o) => o.textContent)).toEqual(['maintainer', 'member', 'viewer'].filter((r) => r !== 'maintainer'))
})

const PK = 'B' + 'x'.repeat(86)
const CERT = JSON.stringify({ o: { device_id: '4'.repeat(32) }, sig: 'demo-not-a-real-signature' })

const DECL = JSON.stringify({ o: { v: 1, kind: 'reader_decl', bind: { via: 'enrol' } }, sig: 'demo-not-a-real-signature' })
function clickOn() {
  state.host.workspaces[0].features = [...READS, ...DO, 'actions']
  clickOnFor(state.host.workspaces[0]) // One click on; fireEvent is untrusted: trustedPress stands in for a real press
}
const openAdd = async (user: ReturnType<typeof userEvent.setup>) => {
  show('/w/DEMO/settings/members')
  await screen.findByRole('list', { name: 'People' }, T)
  await user.click(screen.getByRole('button', { name: /Add person/ }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Name'), 'Nia')
  await user.upload(within(form).getByLabelText('Public key file'), new File([PK + '\n'], 'nia.pk', { type: 'text/plain' }))
  return form
}

it('adding a member is signed with One click and sends the invitee\'s certificate with their One click confirmation', async () => {
  clickOn()
  const user = userEvent.setup()
  const form = await openAdd(user)
  // The certificate and its confirmation chosen together: each lands in its own field, nothing extra to do.
  await user.upload(within(form).getByLabelText('Device certificate file'), [new File([CERT], 'nia.cert.json', { type: 'application/json' }), new File([DECL], 'nia.reader.json', { type: 'application/json' })])
  expect(within(form).getByLabelText('Public key')).toHaveValue(PK + '\n')
  expect(within(form).getByLabelText('Device certificate')).toHaveValue(CERT)
  expect(within(form).getByLabelText('One click confirmation')).toHaveValue(DECL)
  expect(form).toHaveTextContent('Nia needs to confirm they understand One click signatures')
  expect(form).not.toHaveTextContent(/orch keys|reader declare|terminal|--reader-decl/)
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  const dialog = await screen.findByRole('dialog', { name: /Add Nia/ }, T)
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Add Nia' }, T))
  const panel = await within(dialog).findByTestId('click-panel', {}, T)
  expect(panel).toHaveTextContent('FROM-THE-SIGNER member.add')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'member_add', name: 'Nia', role: 'member', pk: PK, cert: CERT, reader_decl: DECL }])
  expect(state.host.posts()).toEqual([]) // no hand-off is ever prepared
  // C3: adding a member is not a daily op: its risk lines show, the press that prepared never signs, the second press does
  expect(within(dialog).getByTestId('sign-risk-lines')).toHaveTextContent('FROM-THE-SIGNER risk: a person is added to this workspace')
  expect(within(dialog).queryByTestId('sign-progress')).toBeNull()
  await new Promise((r) => setTimeout(r, ARM_MS + 200))
  expect(state.host.clickSigned).toEqual([])
  const sign = within(dialog).getByTestId('sign-button')
  await waitFor(() => expect(sign).toBeEnabled(), { timeout: ARM_MS + 3000 })
  trustedPresses()
  fireEvent.pointerDown(sign)
  fireEvent.click(sign)
  await waitFor(() => expect(state.host.clickSigned).toHaveLength(1), T)
})

it('the One click confirmation can be chosen or dropped on its own field', async () => {
  clickOn()
  const user = userEvent.setup()
  const form = await openAdd(user)
  await user.upload(within(form).getByLabelText('Device certificate file'), new File([CERT], 'nia.cert.json', { type: 'application/json' }))
  fireEvent.drop(within(form).getByLabelText('One click confirmation'), { dataTransfer: { files: [new File([DECL], 'nia.reader.json', { type: 'application/json' })] } })
  await waitFor(() => expect(within(form).getByLabelText('One click confirmation')).toHaveValue(DECL))
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  const dialog = await screen.findByRole('dialog', { name: /Add Nia/ }, T)
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Add Nia' }, T))
  await within(dialog).findByTestId('click-panel', {}, T)
  expect(state.host.clickPrepares().map((c) => c.body?.reader_decl)).toEqual([DECL])
})

it('adding a member without their One click confirmation is refused with one plain line and nothing is sent', async () => {
  clickOn()
  const user = userEvent.setup()
  const form = await openAdd(user)
  await user.upload(within(form).getByLabelText('Device certificate file'), new File([CERT], 'nia.cert.json', { type: 'application/json' }))
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['Nia needs to confirm they understand One click signatures. Add the confirmation file their orch app made with the certificate.'])
  // A file that is not one is said so; a certificate dropped on the confirmation goes to the certificate field.
  await user.type(within(form).getByLabelText('One click confirmation'), '{{"v":1}')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['This file is not a One click confirmation. Add the confirmation file their orch app made with the certificate.'])
  expect(state.host.clickPrepares()).toEqual([])
  expect(state.host.posts()).toEqual([])
})

it('a host refusal for a missing confirmation stays plain: no command in the sentence', async () => {
  clickOn()
  const user = userEvent.setup()
  const form = await openAdd(user)
  await user.upload(within(form).getByLabelText('Device certificate file'), [new File([CERT], 'c.json'), new File([DECL], 'd.json')])
  // The host refuses as if the confirmation had not come (e.g. an older dashboard server dropped it).
  state.host.override = (path, method) =>
    path.endsWith('/click/prepare') && method === 'POST'
      ? Response.json({ ok: false, exit: 3, error: { code: 'sign.convenience_readers', message: 'not declared', retryable: false, fix: 'orch member add --reader-decl PATH', detail: { reason: 'no_declaration', devices: [] } } }, { status: 409 })
      : undefined
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  const dialog = await screen.findByRole('dialog', { name: /Add Nia/ }, T)
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Add Nia' }, T))
  await waitFor(() => expect(dialog).toHaveTextContent(READERS_SENTENCE.no_declaration), T)
  expect(dialog).not.toHaveTextContent(/orch member|--reader-decl|terminal/)
})

it('the member form refuses a key or certificate that cannot be one, before any request', async () => {
  const user = userEvent.setup()
  show('/w/DEMO/settings/members')
  await screen.findByRole('list', { name: 'People' }, T)
  await user.click(screen.getByRole('button', { name: /Add person/ }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Name'), 'Nia')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  const noDecl = 'Nia needs to confirm they understand One click signatures. Add the confirmation file their orch app made with the certificate.'
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['Choose or paste their public key.', 'Choose or paste their device certificate.', noDecl])
  await user.type(within(form).getByLabelText('Public key'), 'short')
  await user.type(within(form).getByLabelText('Device certificate'), '[[1')
  await user.click(within(form).getByRole('button', { name: 'Prepare to add' }))
  expect(within(form).getAllByRole('alert').map((a) => a.textContent)).toEqual(['A public key is 87 characters of base64url (65 bytes).', 'The certificate file is not JSON.', noDecl])
  // A file larger than the field takes is refused before it is read.
  await user.upload(within(form).getByLabelText('Device certificate file'), new File(['x'.repeat(16385)], 'big.json', { type: 'application/json' }))
  expect(within(form).getByText(/big.json is larger than 16384 bytes/)).toBeInTheDocument()
  await user.upload(within(form).getByLabelText('One click confirmation file'), new File(['x'.repeat(4097)], 'big-decl.json', { type: 'application/json' }))
  expect(within(form).getByText(/big-decl.json is larger than 4096 bytes/)).toBeInTheDocument()
  expect(state.host.posts()).toEqual([])
  expect(state.host.clickPrepares()).toEqual([])
})

it('Gates: the code review is never approved by the assignees and always independent; the policy goes out in full', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  expect(await screen.findByRole('heading', { name: 'Who approves what?' }, T)).toBeInTheDocument()
  expect(screen.getByText(/A change asks everyone again at that step/)).toBeInTheDocument()
  const matrix = screen.getByRole('table', { name: 'Code review: who may approve' })
  expect(within(matrix).getByRole('checkbox', { name: 'The assignees may not approve' })).toBeChecked()
  expect(within(matrix).getByRole('checkbox', { name: 'The assignees may not approve' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: /Code review: approvers independent.*always/ })).toBeDisabled()
  await user.click(screen.getByRole('radio', { name: 'Every ticket' }))
  await user.click(screen.getByRole('button', { name: 'Change code review policy' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  const panel = await prepareByClick(dialog, 'Change the code review gate')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([
    { action: 'policy_set', gate: 'code', policy: { approvers: ['maintainer', 'owner'], count: 1, not: ['assignees'], applies: 'all', independent: true } },
  ])
  expect(panel).toHaveTextContent('FROM-THE-SIGNER policy.set')
})

it('Gates: another gate changes approvers and count; a policy with no approver is not saved', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Who approves what?' }, T)
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
  await prepareByClick(await screen.findByRole('dialog', {}, T), 'Change the plan gate')
  expect(state.host.clickPrepares()).toHaveLength(1)
  expect(state.host.clickPrepares()[0].body).toMatchObject({ action: 'policy_set', gate: 'plan', policy: { approvers: ['maintainer', 'owner'], count: 2, not: [], applies: 'all', independent: false } })
})

it('Gates: a non-owner reads the policies and cannot change them', async () => {
  state.host.workspaces[0].me = MARA
  state.host.workspaces[0].role = 'maintainer'
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Who approves what?' }, T)
  expect(screen.getByRole('button', { name: 'Change plan policy' })).toBeDisabled()
  expect(screen.getAllByText('Only owners can change who approves.').length).toBe(7) // three questions and four custom rules
  expect(screen.getByLabelText('Who approves the plan?')).toBeDisabled()
  expect(screen.getByRole('switch', { name: 'Require a code review before changes are added' })).toBeDisabled()
})

it('Agent access: an owner stops any active access in one step (a reason, one button), signed with One click', async () => {
  clickOn()
  const user = userEvent.setup()
  show('/w/DEMO/settings/grants')
  expect(await screen.findByRole('heading', { name: 'Agent access' }, T)).toBeInTheDocument()
  expect(await screen.findAllByTestId('grant-card', {}, T)).toHaveLength(2)
  expect(screen.getByRole('article', { name: 'Tom’s agents' })).toHaveTextContent('Claude Code')
  // The one shared agent mark (components/agents/AgentIcon), for both agents.
  const card = screen.getByRole('article', { name: 'Tom’s agents' })
  expect(card.querySelector('[data-agent-icon="claude-code"]')).not.toBeNull()
  expect(card.querySelector('[data-agent-icon="codex"]')).not.toBeNull()
  expect(screen.getByText(GRANT_A)).toBeInTheDocument() // the id is in the closed details
  expect(screen.getByRole('button', { name: 'Stop access for Tom' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Stop access for Tom' }))
  const form = await screen.findByRole('dialog')
  await user.type(within(form).getByLabelText('Why (optional)'), 'no longer needed')
  const panel = await prepareByClick(form, 'Stop access')
  expect(state.host.clickPrepares().map((c) => c.body)).toEqual([{ action: 'grant_revoke', grant: GRANT_B, reason: 'no longer needed' }])
  expect(panel).toHaveTextContent('FROM-THE-SIGNER grant.revoke')
  expect(state.host.posts()).toEqual([])
})

it('Agent access: a member gets the access the host gives them and stops only their own', async () => {
  state.host.workspaces[0].me = TOM
  state.host.workspaces[0].role = 'member'
  state.host.workspaces[0].grants = [grant(GRANT_B, TOM, { scope: 'workable' })] // the host sends a member their own grants only
  show('/w/DEMO/settings/grants')
  expect(await screen.findByText(GRANT_B, {}, T)).toBeInTheDocument()
  expect(screen.queryByText(GRANT_A)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Stop access for Tom' })).toBeEnabled()
  expect(screen.getByText(/Owners see and stop everyone’s access/)).toBeInTheDocument()
})

it('Agent access: nobody has access yet says so and offers "Give agents access"', async () => {
  state.host.workspaces[0].grants = []
  show('/w/DEMO/settings/grants')
  const empty = await screen.findByTestId('no-grants', {}, T)
  expect(empty).toHaveTextContent('Nobody has given agents access.')
  expect(within(empty).getByRole('button', { name: 'Give agents access' })).toBeInTheDocument()
})

it('Agent access: past access is shown on request, with who stopped it only when the host says', async () => {
  const user = userEvent.setup()
  state.host.workspaces[0].grants = [
    grant(GRANT_A, OWNER),
    grant(GRANT_B, TOM, { revoked: { at: '2026-10-12T09:00:00Z', by: MARA, seq: 7 } }),
    grant('gr_' + '0'.repeat(25) + 'C', TOM, { revoked: { at: '2026-10-12T09:30:00Z', by: null, seq: null }, session_ids: null }),
  ]
  show('/w/DEMO/settings/grants')
  await screen.findByText(GRANT_A, {}, T)
  expect(screen.queryByRole('list', { name: 'Past access' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('switch', { name: 'Show past access' }))
  const past = await screen.findByRole('list', { name: 'Past access' }, T)
  const items = within(past).getAllByRole('listitem')
  expect(items).toHaveLength(2)
  expect(items[0]).toHaveTextContent(/Stopped.* by Mara/)
  // No `read_ext/2`: who stopped it is unknown, and nothing is made up.
  expect(items[1]).toHaveTextContent('Stopped')
  expect(items[1]).not.toHaveTextContent(/ by /)
  expect(state.host.calls.some((c) => c.path.endsWith('grants?active=false'))).toBe(true)
  expect(within(past).queryByRole('button')).not.toBeInTheDocument()
})

it('Agent access: the runner grant is marked from grant.list label (H11c-2); another label stays in the details', async () => {
  state.host.workspaces[0].grants = [grant(GRANT_A, OWNER, { label: 'runner' }), grant(GRANT_B, TOM, { label: 'tests' })]
  show('/w/DEMO/settings/grants')
  const runner = (await screen.findByText(GRANT_A, {}, T)).closest('[data-testid="grant-card"]') as HTMLElement
  expect(within(runner).getByTestId('runner-grant-tag')).toHaveTextContent('on this computer')
  const other = screen.getByText(GRANT_B).closest('[data-testid="grant-card"]') as HTMLElement
  expect(within(other).queryByTestId('runner-grant-tag')).not.toBeInTheDocument()
  expect(other).toHaveTextContent('Label tests')
})

it('UP-2: an active runner grant the runner no longer holds says so; one it holds stays Active', async () => {
  state.host.workspaces[0].features = [...state.host.workspaces[0].features, 'agent_start']
  state.host.workspaces[0].grants = [grant(GRANT_A, OWNER, { label: 'runner' }), grant(GRANT_B, OWNER, { label: 'runner' })]
  vi.spyOn(agentsApi, 'runner').mockResolvedValue({ runner: { connected: true, enabled: true, terminals: false, grant: { id: GRANT_B, scope: 'workable', until: '2099-01-01T00:00:00Z' } } })
  show('/w/DEMO/settings/grants')
  const lost = (await screen.findByText(GRANT_A, {}, T)).closest('[data-testid="grant-card"]') as HTMLElement
  await waitFor(() => expect(within(lost).getByTestId('grant-not-held')).toBeInTheDocument(), T)
  expect(lost).toHaveTextContent('Not in use')
  expect(lost).not.toHaveTextContent('Active')
  const held = screen.getByText(GRANT_B).closest('[data-testid="grant-card"]') as HTMLElement
  expect(within(held).queryByTestId('grant-not-held')).not.toBeInTheDocument()
  expect(held).toHaveTextContent('Active')
})

it('host strings with bidi characters are shown escaped, not applied', async () => {
  state.host.workspaces[0].grants = [grant(GRANT_A, OWNER, { scope: 'al\u202el' })]
  show('/w/DEMO/settings/grants')
  const card = (await screen.findByText(GRANT_A, {}, T)).closest('[data-testid="grant-card"]') as HTMLElement
  expect(card.textContent).toContain('\\u{202e}')
  expect(card.textContent).not.toContain('\u202e')
})

it('settings, policy and grant live items refresh the settings queries', async () => {
  const { invalidateLive } = await vi.importActual<typeof import('@/api/useLiveUpdates')>('@/api/useLiveUpdates')
  const { queryClient } = await import('@/production/Shell')
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Who approves what?' }, T)
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
  await screen.findByRole('list', { name: 'People' }, T)
  expect(screen.queryByRole('button', { name: /Add person/ })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Role of Mara')).toBeDisabled()
  expect(state.host.calls.filter((c) => c.method !== 'GET')).toEqual([])
})

void policy

it('Gates: a one-person workspace explains that the verify rule does not block its only member', async () => {
  state.host.workspaces[0].roster = state.host.workspaces[0].roster.slice(0, 1)
  show('/w/DEMO/settings/gates')
  await screen.findByRole('heading', { name: 'Who approves what?' }, T)
  expect(await screen.findByTestId('gate-alone', {}, T)).toHaveTextContent('only member')
})
