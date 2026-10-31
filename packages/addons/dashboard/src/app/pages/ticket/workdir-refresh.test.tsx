import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { workdirApi, workdirQueries } from '@/api/workdir'
import { WorkFilePreview } from './Artifacts'

afterEach(() => vi.restoreAllMocks())
it('a new ticket head refetches the folder and focus always refetches', async () => {
  const get = vi.spyOn(workdirApi, 'get').mockResolvedValue({ available: true, exists: true, codebase: false, items: [] })
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
  function Folder({ seq }: { seq: number }) { const q = useQuery(workdirQueries.ofTicket('w', 'T-1', seq)); return <p>{q.data?.items?.map(i => i.name).join(',')}</p> }
  const view = (seq: number) => <QueryClientProvider client={client}><Folder seq={seq} /></QueryClientProvider>
  const r = render(view(1))
  await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
  get.mockResolvedValue({ available: true, exists: true, codebase: false, items: [{ name: 'hello.txt', bytes: 15 }] })
  r.rerender(view(2))
  expect(await screen.findByText('hello.txt')).toBeInTheDocument()
  expect(get).toHaveBeenCalledTimes(2)
  expect(workdirQueries.ofTicket('w', 'T-1', 2).refetchOnWindowFocus).toBe('always')
})
it.each(['text', 'png', 'big'] as const)('previews %s work files safely', async (kind) => {
  vi.spyOn(workdirApi, 'read').mockResolvedValue(kind === 'big' ? { name: 'hello.txt', bytes: 300000, too_big: true } : kind === 'png' ? { name: 'hello.txt', bytes: 8, kind: 'png', base64: 'aGVsbG8=' } : { name: 'hello.txt', bytes: 15, kind: 'text', text: 'hello from orch' })
  render(<QueryClientProvider client={new QueryClient()}><WorkFilePreview ws="w" ticket="T-1" name="hello.txt" /></QueryClientProvider>)
  if (kind === 'png') expect(await screen.findByRole('img')).toHaveAttribute('src', 'data:image/png;base64,aGVsbG8=')
  else expect(await screen.findByText(kind === 'big' ? /too big to preview/ : 'hello from orch')).toBeInTheDocument()
})
