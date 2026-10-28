import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { MarketList } from '@/api/market'
import { AddMarketplace } from './AddMarketplace'
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('one Add adds a source without approval, signing, installation or Undo', async () => {
  const add = vi.spyOn(api, 'marketAdd').mockResolvedValue({} as Awaited<ReturnType<typeof api.marketAdd>>)
  const prepare = vi.spyOn(api, 'prepareClick')
  const staged = vi.spyOn(api, 'marketStage')
  const onAdded = vi.fn()
  const url = 'https://github.com/example/test-market'
  const list = { offered: [{ url, class: 'official' }], open_address: false } as MarketList
  render(<QueryClientProvider client={new QueryClient()}><AddMarketplace ws="w" markets={[]} list={list} onAdded={onAdded} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: '+ Add marketplace' }))
  expect(screen.getByText(/Repository:/)).toHaveTextContent('example/test-market')
  fireEvent.click(screen.getByRole('button', { name: 'Add' }))
  await waitFor(() => expect(onAdded).toHaveBeenCalledWith(url))
  expect(add).toHaveBeenCalledTimes(1)
  expect(add).toHaveBeenCalledWith('w', url)
  expect(prepare).not.toHaveBeenCalled()
  expect(staged).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: /Approve|Undo/ })).toBeNull()
  expect(document.body).not.toHaveTextContent('orch sign')
})
