import { screen, waitFor, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { fakeRequest, state } from '@/test/handoffHarness'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// Reuse the host hand-off harness: signing itself is covered by the shared form tests.
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { title: string; onSettled?: (phase: string, st: unknown, req: unknown) => void; onClose: () => void }) => {
    const req = { ...fakeRequest({ key: 'DEMO-0044', id: 'hr_' + '0'.repeat(25) + '7' }), op: 'new' }
    return <div role="dialog" aria-label={p.title}>
      <button onClick={() => p.onSettled?.('done', state(req, { state: 'submitted', key: 'DEMO-0044' }), req)}>signed</button>
      <button onClick={p.onClose}>cancelled</button>
    </div>
  },
}))

it('keeps the signed create flow in Focus after creation succeeds', async () => {
  const { user, router } = renderApp('/focus', { setup: (store) => installAndGrant(store, store.workspaces[0].id, 'focus') })
  await user.click(await screen.findByRole('button', { name: 'New ticket with me' }))
  const dialog = await screen.findByRole('dialog', { name: 'New ticket' })
  await user.type(await within(dialog).findByLabelText('Title'), 'Signed focus ticket')
  await user.type(within(dialog).getByLabelText(/^What should be true/), 'Keep this session in focus')
  await user.click(within(dialog).getByRole('button', { name: 'Create ticket' }))
  await user.click(await screen.findByRole('button', { name: 'signed' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/focus/DEMO-0044'))
  expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull()
})
