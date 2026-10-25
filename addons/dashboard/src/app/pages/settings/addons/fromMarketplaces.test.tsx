import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { installedAddon } from '@/test/fixtures/market'
import { renderApp } from '@/test/renderApp'

afterEach(() => vi.restoreAllMocks())

it('From marketplaces: the same chip, and closing the panel puts focus back on the row’s Settings button', async () => {
  vi.spyOn(api, 'marketInstalled').mockResolvedValue({ addons: [installedAddon({ name: 'calendar' })] })
  const { user } = renderApp('/settings/addons')
  await screen.findByRole('row', { name: /Publish/ }, { timeout: 8000 })
  const section = await screen.findByRole('region', { name: 'From marketplaces' }, { timeout: 8000 })
  expect(within(section).getByText('Off')).toHaveAttribute('data-state-chip', 'off')
  await user.click(within(section).getByRole('button', { name: 'Settings' }))
  const panel = await screen.findByRole('dialog', { name: /calendar settings/i })
  await user.click(await within(panel).findByRole('button', { name: 'Cancel' }, { timeout: 8000 }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(within(screen.getByRole('region', { name: 'From marketplaces' })).getByRole('button', { name: 'Settings' })).toHaveFocus())
}, 30_000)
