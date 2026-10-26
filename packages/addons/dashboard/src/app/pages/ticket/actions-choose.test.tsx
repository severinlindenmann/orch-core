import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { ActionsMenu, type StartAgent } from './Header'

afterEach(() => vi.restoreAllMocks())

// Run 8, V2: on a real host the Actions menu offers the same dialog in choose mode next to Start agent.
const viewer = { person: 'p_sev', role: 'owner' as const, members: [{ person: 'p_sev', name: 'Severin', role: 'owner' as const }], name: (id: string | null | undefined) => (id === 'p_sev' ? 'Severin' : String(id)), ready: true }
const start = (over: Partial<StartAgent> = {}): StartAgent => ({ shown: true, reason: null, startable: true, run: vi.fn(), choose: vi.fn(), node: null, ...over })
const open = async (s: StartAgent) => {
  const ticket = structuredClone(mockStore.ticket('DEMO-0043')!)
  render(<ActionsMenu ticket={ticket} viewer={viewer as never} sign={vi.fn()} jump={vi.fn()} start={s} />)
  await userEvent.click(screen.getByRole('button', { name: /Actions/ }))
}

it('"Change agent or model…" in Actions opens choose mode, not the instant start', async () => {
  const s = start()
  await open(s)
  await userEvent.click(await screen.findByRole('menuitem', { name: 'Change agent or model…' }))
  expect(s.choose).toHaveBeenCalledTimes(1)
  expect(s.run).not.toHaveBeenCalled()
})

it('"Change agent or model…" is off with the same reason as Start agent, and absent without a real host', async () => {
  const s = start({ reason: 'An agent is already working on this ticket' })
  await open(s)
  expect(await screen.findByRole('menuitem', { name: 'Change agent or model…' })).toHaveAttribute('aria-disabled', 'true')
  await userEvent.keyboard('{Escape}')
})

it('no choose item when the host gives no choose mode (the mock)', async () => {
  await open(start({ choose: undefined }))
  expect(screen.queryByRole('menuitem', { name: 'Change agent or model…' })).toBeNull()
})
