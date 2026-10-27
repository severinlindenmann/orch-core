import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import { ApiError, type Cosmetic } from '@/api/types'
import { useCosmetic } from './useCosmetic'

const { setCosmetic, toast } = vi.hoisted(() => ({ setCosmetic: vi.fn(), toast: vi.fn() }))
vi.mock('@/api/client', () => ({ api: { setCosmetic } }))
vi.mock('sonner', () => ({ toast }))
vi.mock('@/app/toast', () => ({ toastApiError: vi.fn() }))

const WS = { id: 'w1', name: 'Old name', icon: null as Cosmetic['icon'] }
const STAR = { icon: 'star', colour: 'teal' }

function mount() {
  const client = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return renderHook(() => useCosmetic(WS), { wrapper })
}
/** The "Undo" of the last "Saved" toast. */
const undo = () => (toast.mock.calls.find((c) => c[0] === 'Saved')![1] as { action: { onClick: () => void } }).action.onClick()

beforeEach(() => {
  setCosmetic.mockReset()
  toast.mockReset()
})

it('saves against the shown values and Undo sends the old ones against the new values', async () => {
  setCosmetic.mockResolvedValueOnce({ name: 'New name', icon: null }).mockResolvedValueOnce({ name: 'Old name', icon: null })
  const { result } = mount()
  await act(async () => {
    await result.current.save({ name: 'New name' })
  })
  expect(setCosmetic).toHaveBeenCalledWith('w1', { name: 'New name', if_current: { name: 'Old name', icon: null } })
  expect(toast).toHaveBeenCalledWith('Saved', { action: { label: 'Undo', onClick: expect.any(Function) }, duration: 10000 })
  act(() => undo())
  await waitFor(() => expect(setCosmetic).toHaveBeenCalledTimes(2))
  expect(setCosmetic).toHaveBeenLastCalledWith('w1', { name: 'Old name', if_current: { name: 'New name', icon: null } })
})

it('says so when someone changed it meanwhile, and Undo sends only the icon', async () => {
  setCosmetic
    .mockResolvedValueOnce({ name: 'Old name', icon: STAR })
    .mockRejectedValueOnce(new ApiError(409, { code: 'cosmetic.conflict', message: 'stale', retryable: false }))
  const { result } = mount()
  await act(async () => {
    await result.current.save({ icon: STAR })
  })
  act(() => undo())
  await waitFor(() => expect(toast).toHaveBeenCalledWith('Someone changed it meanwhile'))
  expect(setCosmetic).toHaveBeenLastCalledWith('w1', { icon: null, if_current: { name: 'Old name', icon: STAR } })
})
