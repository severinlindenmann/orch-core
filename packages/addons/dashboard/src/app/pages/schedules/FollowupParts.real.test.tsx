import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { SignPopoverProps } from '@/components/sign/SignPopover'
import type { FollowupRow } from '@/api/followups'
import { quickPicks } from '@/api/followups'
import { addonStateKey } from '@/api/queries'
import { fmtExact } from '@/lib/time'
import { CancelButton, FollowupUndoHost, RescheduleButton } from './FollowupParts'

const mocks = vi.hoisted(() => ({ sign: vi.fn(), toast: vi.fn(), addon: vi.fn() }))
vi.mock('@/api/features', () => ({ isRealHost: () => true, hasFeature: () => true }))
vi.mock('@/api/client', () => ({ api: { runAddonAction: mocks.addon } }))
vi.mock('sonner', () => ({ toast: Object.assign(mocks.toast, { success: vi.fn() }) }))
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (props: SignPopoverProps) => { mocks.sign(props); return <div data-testid="sign-popover" /> },
}))

const row: FollowupRow = {
  id: 'fu_' + '0'.repeat(26), ticket: 'DEMO-0001', title: 'Check',
  state: 'pending', trigger: 'time', due: null, deadline: null, set_at: null, fired_at: null,
  late_by: null, brief: null, outcome: null, reason: null, running: false,
  person: 'p_me', mine: true, can_cancel: true, can_reschedule: true,
}
const ws = 'workspace'
const props = () => mocks.sign.mock.lastCall![0] as SignPopoverProps
beforeEach(() => vi.clearAllMocks())

it('a real-host reschedule opens the one-click signing popover with the exact picked instant', () => {
  render(<QueryClientProvider client={new QueryClient()}><RescheduleButton row={row} ws={ws} canMove /></QueryClientProvider>)
  const pick = quickPicks()[0]
  fireEvent.click(screen.getByRole('button', { name: 'Reschedule' }))
  fireEvent.click(screen.getByRole('button', { name: pick.label }))
  expect(screen.getByTestId('sign-popover')).toBeInTheDocument()
  expect(props()).toMatchObject({
    ws, action: { action: 'followup_reschedule', key: row.ticket, id: row.id, when: pick.at.toISOString() },

    doneToast: `Moved the follow-up on ${row.ticket} to ${fmtExact(pick.at.toISOString())}.`,
  })
  expect(mocks.addon).not.toHaveBeenCalled()
  expect(mocks.toast).not.toHaveBeenCalled()
})

it('a successful cancel offers Undo after the card unmounts and refreshes the shared queries', () => {
  const qc = new QueryClient()
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  const view = (card: boolean) => <QueryClientProvider client={qc}><FollowupUndoHost />{card && <CancelButton row={row} ws={ws} canUndo />}</QueryClientProvider>
  const { rerender } = render(view(true))
  fireEvent.click(screen.getByRole('button', { name: 'Cancel follow-up' }))
  const cancel = props()
  act(() => cancel.onSettled?.('failed', null, null))
  expect(mocks.toast).not.toHaveBeenCalled()
  act(() => cancel.onSettled?.('done', null, null))
  expect(mocks.toast).toHaveBeenCalledWith('Cancelled the follow-up on DEMO-0001.', expect.objectContaining({ duration: 15_000 }))
  const undo = mocks.toast.mock.lastCall![1].action
  rerender(view(false))
  expect(screen.queryByTestId('sign-popover')).not.toBeInTheDocument()
  act(() => undo.onClick())
  expect(props()).toMatchObject({
    ws, action: { action: 'followup_undo', key: row.ticket, id: row.id },
      doneToast: 'Follow-up restored.',
  })
  invalidate.mockClear()
  act(() => props().onSettled?.('done', null, null))
  expect(invalidate.mock.calls.map(([arg]) => arg?.queryKey)).toEqual([
    ['followups', ws], addonStateKey(ws, 'schedules'), ['tickets', ws],
  ])
})

it('reschedule is hidden when the row cannot be moved', () => {
  render(<QueryClientProvider client={new QueryClient()}><RescheduleButton row={{ ...row, can_reschedule: false }} ws={ws} canMove /></QueryClientProvider>)
  expect(screen.queryByRole('button', { name: 'Reschedule' })).not.toBeInTheDocument()
})
