import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { CLICK_RISK } from '@/api/click'
import { ApprovalMethods } from './ApprovalMethods'
const ask = vi.fn()
vi.mock('./useSettingsHandoff', () => ({ useSettingsHandoff: () => ({ ask, dialog: null }) }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); ask.mockReset() })
function mount(owner = true, supported = true) {
  vi.spyOn(api, 'getHostSettings').mockResolvedValue((supported ? { sign_methods: ['terminal', 'click', 'phone'] } : {}) as Awaited<ReturnType<typeof api.getHostSettings>>)
  vi.spyOn(api, 'getSignDefault').mockImplementation(async () => { if (!supported) throw new Error('Absent'); return { method: 'click', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false } })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ApprovalMethods ws="w" owner={owner} /></QueryClientProvider>)
}
it('saves allowed methods through the signed settings flow and keeps reserved methods disabled', async () => {
  mount()
  const phone = screen.getByLabelText('Phone (Face ID)')
  await waitFor(() => expect(phone).toBeEnabled())
  expect(screen.getByLabelText('Terminal (always)')).toBeDisabled()
  expect(screen.getByLabelText(/Touch ID/)).toBeDisabled()
  expect(screen.getByLabelText(/Security key/)).toBeDisabled()
  expect(screen.getByTestId('click-risk')).toHaveTextContent(CLICK_RISK)
  fireEvent.click(phone)
  fireEvent.click(screen.getByRole('button', { name: 'Save allowed methods' }))
  expect(ask).toHaveBeenCalledWith(expect.objectContaining({ action: { action: 'settings_set', sign_methods: ['terminal', 'click'] } }))
})
it('saves personal default and fallback without a workspace signature', async () => {
  const put = vi.spyOn(api, 'putSignDefault').mockResolvedValue({ method: 'phone', fallback: 'terminal', allowed: ['terminal', 'click', 'phone'], is_default: false })
  mount(false)
  await waitFor(() => expect(screen.getByLabelText('Default')).toBeEnabled())
  expect(screen.getByLabelText('Phone (Face ID)')).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Default'), { target: { value: 'phone' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save my preference' }))
  await screen.findByText('Preference saved.')
  expect(put).toHaveBeenCalledWith('w', { method: 'phone', fallback: 'terminal' })
  expect(ask).not.toHaveBeenCalled()
})
it('is read-only with an explanation when the APIs are absent', async () => {
  mount(true, false)
  await screen.findAllByText(/These controls are read-only/)
  expect(screen.getByLabelText('Default')).toBeDisabled()
  expect(screen.getByLabelText('One click on this computer')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save my preference' })).toBeDisabled()
})
