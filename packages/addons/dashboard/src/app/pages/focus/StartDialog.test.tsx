import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as features from '@/api/features'
import { runsApi, type Run, type TermPreview } from '@/api/runs'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces[0].id, 'focus')

it('shows all three choices inline and starts scratch', async () => {
  const { user, router } = renderApp('/focus', { setup })
  const start = await screen.findByTestId('focus-start')
  expect(within(start).getByRole('heading', { name: 'Start focus session' })).toBeInTheDocument()
  expect(within(start).getByRole('button', { name: /^New ticket.*together/ })).toBeInTheDocument()
  expect(within(start).getByRole('button', { name: /^Existing ticket/ })).toBeInTheDocument()
  await user.click(within(start).getByRole('button', { name: /^Scratch session/ }))
  await waitFor(() => expect(router.state.location.search).toMatchObject({ scratch: 1 }))
  expect(await screen.findByTestId('focus-conversation')).toBeInTheDocument()
  expect(screen.queryByTestId('focus-start')).toBeNull()
})

it('picks an open ticket and navigates to its session', async () => {
  const { user, router } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: /^Existing ticket/ }))
  const picker = await screen.findByRole('combobox', { name: 'Open tickets' })
  for (const option of within(picker).getAllByRole('option')) {
    const key = (option as HTMLOptionElement).value
    if (key) expect(['done', 'closed']).not.toContain(mockStore.ticket(key)?.status)
  }
  await user.selectOptions(picker, 'DEMO-0043')
  await waitFor(() => expect(router.state.location.pathname).toBe('/focus/DEMO-0043'))
  await waitFor(() => expect(screen.getByTestId('focus-ticket-chip')).toHaveTextContent('DEMO-0043'))
})

it('creates a ticket through the existing form then opens its focus session', async () => {
  const { user, router } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: /^New ticket.*together/ }))
  const dialog = await screen.findByRole('dialog', { name: 'New ticket' })
  await user.type(await within(dialog).findByLabelText('Title'), 'Focus entry ticket')
  await user.type(within(dialog).getByLabelText(/^What should be true/), 'Keep the work in focus')
  await user.click(within(dialog).getByRole('button', { name: 'Create' }))
  await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/focus\/DEMO-\d+$/))
  const key = router.state.location.pathname.split('/').at(-1)!
  expect(mockStore.ticket(key)?.title).toBe('Focus entry ticket')
  await waitFor(() => expect(screen.getByTestId('focus-ticket-chip')).toHaveTextContent('Focus entry ticket'))
})

afterEach(() => vi.restoreAllMocks())
const preview = (over: Partial<TermPreview> = {}): TermPreview => ({ spec: 'a'.repeat(64), display: 'claude', display_hidden: false, cwd_path: null, cwd_path_hidden: false, notice: null, as_person: true, login_shell: false, env: [], blocked: null, warnings: [], ...over })

it('on a real host "No ticket" previews and opens a person-run Claude terminal, then shows it as scratch', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  const prev = vi.spyOn(runsApi, 'preview').mockResolvedValue(preview())
  const open = vi.spyOn(runsApi, 'open').mockResolvedValue({ run: { run: 'r_SCRATCH1' } as Run })
  vi.spyOn(runsApi, 'list').mockResolvedValue({ runs: [], runner: null, truncated: false })
  const { user, router } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: /^Scratch session/ }))
  expect(await screen.findByTestId('focus-scratch-preview')).toHaveTextContent('Starts Claude as you')
  expect(prev).toHaveBeenCalledWith(mockStore.workspaces[0].id, { harness: 'claude-code', cwd: { kind: 'workspace' } })
  expect(open).not.toHaveBeenCalled()
  await user.click(within(screen.getByTestId('focus-scratch-preview')).getByRole('button', { name: 'Start' }))
  await waitFor(() => expect(router.state.location.search).toMatchObject({ scratch: 'r_SCRATCH1' }))
  expect(open).toHaveBeenCalledWith(mockStore.workspaces[0].id, { harness: 'claude-code', cwd: { kind: 'workspace' } }, 'a'.repeat(64), expect.stringMatching(/^a_/))
  expect(screen.queryByTestId('focus-start')).toBeNull()
})

it('a blocked preview cannot be started', async () => {
  vi.spyOn(features, 'isRealHost').mockReturnValue(true)
  vi.spyOn(runsApi, 'preview').mockResolvedValue(preview({ blocked: { code: 'runner.disabled', message: 'Terminals are off here.' } }))
  const open = vi.spyOn(runsApi, 'open')
  const { user } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: /^Scratch session/ }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Terminals are off here.')
  expect(within(screen.getByTestId('focus-scratch-preview')).getByRole('button', { name: 'Start' })).toBeDisabled()
  expect(open).not.toHaveBeenCalled()
})
