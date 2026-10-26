// Codex integration review #1: after the host refuses a decision answer (409: closed, changed, stale), core refetches
// the decisions, so the next choice snapshots the current decision instead of re-signing the cached one.
import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
afterEach(() => vi.restoreAllMocks())

it('a 409 on a decision answer refetches the decisions', async () => {
  const { user } = renderApp('/', { viewer: 'p_sev' })
  const row = await screen.findByTestId('card-addon:dec_publish_failed_build', {}, T)
  const decide = within(row).queryByRole('button', { name: 'Decide' })
  if (decide?.getAttribute('aria-expanded') === 'false') await user.click(decide)
  const fetches = vi.spyOn(api, 'getAddonDecisions')
  vi.spyOn(api, 'runAddonAction').mockRejectedValueOnce(new ApiError(409, { code: 'decision.closed', message: 'This decision changed since you opened it.', retryable: false }))
  await user.click(within(row).getByRole('button', { name: 'Retry last good version' }))
  const dialog = await screen.findByRole('dialog', { name: 'Decide for Publish (publish)' }, T)
  const before = fetches.mock.calls.length
  await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
  await waitFor(() => expect(fetches.mock.calls.length).toBeGreaterThan(before), T)
}, 30_000)
