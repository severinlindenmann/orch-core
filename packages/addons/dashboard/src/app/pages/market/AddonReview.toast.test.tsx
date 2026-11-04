// W07 / W14: what the person is told once an add-on switch is signed.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Workspace } from '@/api/types'
import type { AddOnRow } from '@/api/addOnSwitches'
import { AddonReview } from './AddonReview'

const seen: { doneToast?: string }[] = []
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { doneToast?: string; onSettled?: (phase: string) => void }) => {
    seen.push({ doneToast: p.doneToast })
    queueMicrotask(() => p.onSettled?.('done'))
    return null
  },
}))
vi.mock('../settings/hub/shared', () => ({ useHub: () => ({ data: undefined }) }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); seen.length = 0 })

const row = (state: 'on' | 'off'): AddOnRow => ({ id: 'usage', title: 'Usage', desc: '', state, locked: null, blocked: null, version: '0.1.0' } as AddOnRow)
const mount = (r: AddOnRow, browse?: boolean) => render(<QueryClientProvider client={new QueryClient()}><AddonReview workspace={{ id: 'ws' } as Workspace} row={r} marketplace={false} browse={browse} onBusy={() => {}} onDone={() => {}} onClose={() => {}} /></QueryClientProvider>)

describe('the add-on switch says what happened', () => {
  it('W14: turning on from Browse says it is on and where it went (no bare "signed")', async () => {
    const ok = vi.spyOn(toast, 'success')
    mount(row('off'), true)
    await vi.waitFor(() => expect(ok).toHaveBeenCalled())
    expect(seen[0].doneToast).toBe('')
    expect(ok.mock.calls[0][0]).toBe('Usage is on. You find it under Installed now.')
  })
  it('W14: turning on elsewhere says it is on', async () => {
    const ok = vi.spyOn(toast, 'success')
    mount(row('off'))
    await vi.waitFor(() => expect(ok).toHaveBeenCalled())
    expect(ok.mock.calls[0][0]).toBe('Usage is on.')
  })
  it('W07: turning off says it is off, with Undo', async () => {
    const ok = vi.spyOn(toast, 'success')
    mount(row('on'))
    await vi.waitFor(() => expect(ok).toHaveBeenCalled())
    expect(ok.mock.calls[0][0]).toBe('Usage is off.')
    expect((ok.mock.calls[0][1] as { action: { label: string } }).action.label).toBe('Undo')
  })
})
