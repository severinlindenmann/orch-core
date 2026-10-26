// Core H12d-2 in the production app on a fake server: the signer's `readers_old` on Settings → General, a terminal
// hand-off refused with `sign.convenience_readers` (its reason in plain words, its device by name, never by id), and
// Settings > Devices reading `/readers`.
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { READERS_SENTENCE, SIGNER_PROBLEM_SENTENCE } from '@/api/refusals'
import { DO, MARA, OWNER, READS, show, useFakeHost, workspace } from '@/test/hostHarness'

vi.mock('@/api/client', async () => await import('@/api/client.prod'))
vi.mock('@/api/bootstrap', () => ({ getToken: () => 'demo-not-a-real-token', clearSession: vi.fn(), onSessionChange: () => () => {} }))
vi.mock('@/api/useLiveUpdates', async (original) => ({ ...(await original<typeof import('@/api/useLiveUpdates')>()), useLiveUpdates: vi.fn(() => ({ ready: true, revoked: false })) }))

const A = 'a'.repeat(32)
const T = { timeout: 5000 }
const MARA_DEVICE = 'd_' + '2'.repeat(32)
const state = useFakeHost(() => [workspace({ id: A, features: [...READS, ...DO, 'actions', 'readers'] })])

it('readers_old: the Signing card says why in its own sentence and links to Settings → Members', async () => {
  state.host.workspaces[0].click = { custody: 'convenience', enabled: true, available: false, problem: 'readers_old' }
  show('/w/DEMO/settings/general')
  const card = await screen.findByTestId('workspace-card-signing', {}, T)
  expect(card).toHaveTextContent(SIGNER_PROBLEM_SENTENCE.readers_old)
  expect(card).not.toHaveTextContent('(readers_old)')
  const link = within(card).getByRole('link', { name: 'A device needs an update' })
  expect(link.getAttribute('href')).toMatch(/\/settings\/members$/)
})

it('a hand-off refused with sign.convenience_readers says the reason in plain words and names the device, not its id', async () => {
  state.host.override = (path, method) =>
    path.endsWith('/handoff') && method === 'POST'
      ? Response.json(
          { ok: false, exit: 3, error: { code: 'sign.convenience_readers', message: 'reader_too_old: the reader is below required_reader', retryable: false, detail: { reason: 'reader_too_old', devices: [MARA_DEVICE] } } },
          { status: 409 },
        )
      : undefined
  const user = userEvent.setup()
  show('/w/DEMO/settings/general')
  const input = await screen.findByRole('combobox', { name: 'Longest agent access you can give' }, T)
  await user.selectOptions(input, '24')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  const dialog = await screen.findByRole('dialog', {}, T)
  const end = await within(dialog).findByText(READERS_SENTENCE.reader_too_old, {}, T)
  const box = end.closest('[data-handoff-end]') as HTMLElement
  expect(box).toHaveAttribute('data-handoff-end', 'error')
  expect(await within(box).findByText(/device \(unnamed\)/, {}, T)).toHaveTextContent('Mara’s device (unnamed)')
  expect(within(box).getByTestId('refusal-devices').textContent).not.toContain(MARA_DEVICE)
  expect(within(box).getByTestId('refusal-host-message')).toHaveTextContent('reader_too_old: the reader is below')
})

it('Settings → Devices reads /readers: levels, required_reader and the device below it', async () => {
  state.host.override = (path) =>
    path.endsWith('/readers')
      ? Response.json({
          available: true,
          required_reader: 1,
          readers: [
            { device: 'd_' + '1'.repeat(32), person: OWNER, reader: 1, level: 1 },
            { device: MARA_DEVICE, person: MARA, reader: 0, level: 0 },
          ],
        })
      : undefined
  show('/w/DEMO/settings/devices')
  const rows = await screen.findAllByTestId('reader-row', {}, T)
  expect(rows.map((r) => r.dataset.below)).toEqual(['true', 'false'])
  expect(rows[0]).toHaveTextContent('Mara’s device (unnamed)')
  expect(rows[0]).toHaveTextContent('Needs an update of the orch app')
  expect(screen.getByTestId('readers-required')).toHaveAttribute('data-required', '1')
  expect(state.host.calls.some((c) => c.path.endsWith('/readers'))).toBe(true)
})
