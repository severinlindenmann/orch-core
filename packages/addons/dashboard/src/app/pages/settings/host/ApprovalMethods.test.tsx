import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApprovalMethods } from './ApprovalMethods'
const ask = vi.fn()
vi.mock('./useSettingsHandoff', () => ({ useSettingsHandoff: () => ({ ask, dialog: null }) }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); ask.mockReset() })
function mount(owner = true, supported: boolean | null = true, custody: 'convenience' | 'passphrase' | 'none' = 'convenience', signed: string[] = ['terminal', 'click', 'phone']) {
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: 'w', click: { custody, enabled: custody === 'convenience', available: true } }] as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>)
  vi.spyOn(api, 'getHostSettings').mockResolvedValue((supported === null ? { sign_methods: null } : supported ? { sign_methods: signed } : {}) as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockImplementation(async () => { if (supported === false) throw new Error('Absent'); return { method: 'click', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false } })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ApprovalMethods ws="w" owner={owner} /></QueryClientProvider>)
}
it('saves allowed methods through the signed settings flow and keeps reserved methods disabled', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 1 })
  mount()
  const phone = await screen.findByLabelText('Phone (Face ID)')
  await waitFor(() => expect(phone).toBeEnabled())
  await waitFor(() => expect(screen.getByLabelText('One click on this computer')).toBeChecked())
  expect(screen.getByLabelText('Terminal (always)')).toBeDisabled()
  expect(screen.queryByLabelText(/Touch ID/)).toBeNull() // not offered at all until it exists
  expect(screen.queryByLabelText(/Security key/)).toBeNull()
  expect(screen.queryByTestId('click-risk')).toBeNull() // the risk line lives on the Sign in the dashboard card only
  fireEvent.click(phone)
  fireEvent.click(screen.getByRole('button', { name: 'Save allowed methods' }))
  expect(ask).toHaveBeenCalledWith(expect.objectContaining({ action: { action: 'settings_set', sign_methods: ['terminal', 'click'] } }))
})
it('keeps hub in the saved list when the workspace approves through a hub (no box for it)', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 1 })
  mount(true, true, 'convenience', ['terminal', 'click', 'phone', 'hub'])
  const phone = await screen.findByLabelText('Phone (Face ID)')
  await waitFor(() => expect(screen.getByTestId('hub-approvals')).toBeTruthy())
  fireEvent.click(phone)
  fireEvent.click(screen.getByRole('button', { name: 'Save allowed methods' }))
  expect(ask).toHaveBeenCalledWith(expect.objectContaining({ action: { action: 'settings_set', sign_methods: ['terminal', 'click', 'hub'] } }))
})
it('saves personal default and fallback without a workspace signature', async () => {
  const put = vi.spyOn(api, 'putSignDefault').mockResolvedValue({ method: 'terminal', fallback: 'click', allowed: ['terminal', 'click', 'phone'], is_default: false })
  mount(false)
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
  expect(await screen.findByText(/Phone: no phone is paired yet|Phone: add the Phone add-on first/)).toBeInTheDocument()
  expect(screen.queryByLabelText('Phone (Face ID)')).toBeNull() // not set up: no checkbox that looks usable
  fireEvent.change(screen.getByLabelText('Default'), { target: { value: 'terminal' } })
  fireEvent.change(screen.getByLabelText('Fallback'), { target: { value: 'click' } })
  expect(screen.queryAllByRole('option', { name: /Approve on your phone/ })).toHaveLength(0) // phone is no personal default yet
  fireEvent.click(screen.getByRole('button', { name: 'Save my preference' }))
  await screen.findByText('Preference saved.')
  expect(put).toHaveBeenCalledWith('w', { method: 'terminal', fallback: 'click' })
  expect(ask).not.toHaveBeenCalled()
})
it('hides the personal controls and says one line when the host gives no preferences', async () => {
  mount(true, false)
  await screen.findByText(/Your own way of approving is not available on this host yet/)
  expect(screen.queryByLabelText('Default')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Save my preference' })).toBeNull()
  expect(await screen.findByLabelText('One click on this computer')).toBeDisabled()
})

it('shows the personal controls once the host serves preferences, and none of them before', async () => {
  mount(true, true)
  expect(screen.queryByLabelText('Default')).toBeNull() // still loading: no working-looking controls
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
})

it('does not show Phone as allowed in an unrestricted workspace without a paired phone', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 0 })
  mount(true, null)
  await screen.findByText(/No limit is set/)
  expect(screen.queryByLabelText('Phone (Face ID)')).toBeNull()
  expect(await screen.findByText(/add the Phone add-on first/)).toBeInTheDocument()
  await waitFor(() => expect(screen.getByLabelText('One click on this computer')).toBeChecked())
})

it('N11: on a terminal workspace One click is off and not offered, and the boxes follow the signed methods', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 0 })
  mount(true, null, 'none')
  await screen.findByText(/No limit is set/)
  await waitFor(() => expect(screen.getByTestId('click-next-step')).toBeInTheDocument())
  expect(screen.queryByLabelText('One click on this computer')).toBeNull() // not set up: no checkbox that looks usable
  expect(screen.queryByLabelText('Phone (Face ID)')).toBeNull()
  expect(screen.getByTestId('click-next-step')).toHaveTextContent('One click is not set up on this computer')
  expect(screen.getByText('Phone: add the Phone add-on first.')).toBeInTheDocument()
  expect(screen.queryByText(/not available on this host/)).toBeNull()
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
  // My settings offers no one-click fallback either.
  expect(screen.queryByRole('option', { name: /One click/i })).toBeNull()
})

it('N11: a signed list of terminal only leaves One click unchecked even with convenience custody', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 1 })
  vi.spyOn(api, 'getHostSettings').mockResolvedValue({ sign_methods: ['terminal'] } as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'terminal', allowed: ['terminal'], is_default: true } as Awaited<ReturnType<typeof api.getSignDefault>>)
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: 'w', click: { custody: 'convenience', enabled: true, available: true } }] as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ApprovalMethods ws="w" owner /></QueryClientProvider>)
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
  expect(screen.getByLabelText('One click on this computer')).not.toBeChecked()
  expect(screen.getByLabelText('Phone (Face ID)')).not.toBeChecked()
  expect(screen.getByLabelText('Terminal (always)')).toBeChecked()
})

it('treats explicit null as unrestricted and lets the owner set a restriction', async () => {
  vi.spyOn(api, 'phoneAvailable').mockResolvedValue({ phones: 1 })
  mount(true, null)
  await screen.findByText(/No limit is set/)
  await waitFor(() => expect(screen.getByLabelText('Phone (Face ID)')).toBeChecked())
  for (const label of ['Terminal (always)', 'One click on this computer']) expect(screen.getByLabelText(label)).toBeChecked()
  expect(screen.getByLabelText('Default')).toBeEnabled()
  fireEvent.click(screen.getByLabelText('One click on this computer'))
  fireEvent.click(screen.getByRole('button', { name: 'Save allowed methods' }))
  expect(ask).toHaveBeenCalledWith(expect.objectContaining({ action: { action: 'settings_set', sign_methods: ['terminal', 'phone'] } }))
})
it('#1115: with one click on and nothing stored, the form shows the effective default (One click), not Terminal', async () => {
  mount(false)
  vi.spyOn(api, 'getSignDefault').mockResolvedValue({ method: 'click', allowed: ['terminal', 'click', 'phone'], is_default: true })
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
  await waitFor(() => expect((screen.getByLabelText('Default') as HTMLSelectElement).value).toBe('click'))
})
