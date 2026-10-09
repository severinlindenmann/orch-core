import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { Overview } from '../Overview'
import type { TabProps } from '../shared'

// A widget that throws for one block body; the boundary around it must recover once the block changes.
vi.mock('./WidgetBlock', () => ({
  WidgetBlock: ({ block }: { block: { raw: string } }) => {
    if (block.raw.includes('BOOM')) throw new Error('boom')
    return <p>widget drawn</p>
  },
}))

const fence = (body: string) => '```orch\n' + body + '\n```'
const withContext = (t: TicketDocument, context: string): TicketDocument => ({ ...t, body: { ...t.body, context } })

describe('per-widget error boundary on the Overview', () => {
  it('a failed widget recovers when a live update (or another ticket) brings a different block', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    resetMockStoreForTests()
    const base = mockStore.ticket('DEMO-0046')!
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (t: TicketDocument) => (
      <QueryClientProvider client={client}>
        <Overview {...({ ticket: t, jump: () => {}, sign: () => {} } as unknown as TabProps)} />
      </QueryClientProvider>
    )
    const bad = withContext(base, `Intro.\n\n${fence('{"type":"kv","id":"k","items":{"a":"BOOM"}}')}`)
    const { rerender } = render(view(bad))
    expect(screen.getByText(/This widget could not be drawn/)).toBeInTheDocument()
    rerender(view(withContext(base, `Intro.\n\n${fence('{"type":"kv","id":"k","items":{"a":"fixed"}}')}`)))
    expect(screen.queryByText(/This widget could not be drawn/)).toBeNull()
    expect(screen.getAllByText('widget drawn').length).toBeGreaterThan(0)
    vi.restoreAllMocks()
  })
})
