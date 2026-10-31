import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { queries } from '@/api/queries'
import { shareQueries, type Staged, type SharesData } from '@/api/shares'
import type { SignContent } from '@/api/signing'
import type { ReactNode } from 'react'
import { ShareFlow } from './ShareFlow'

const complete: SignContent = {
  headline: 'Share these files',
  summary: { title: 'Share these files', lines: ['Anyone who has the link can open the files, for 7 days.', 'The files go to this computer; the link opens there too.'] },
}
let content: SignContent | undefined
beforeEach(() => { content = complete })
vi.mock('@/components/sign/SignPopover', () => ({ SignPopover: (p: { lead: (c: SignContent | undefined) => ReactNode }) => <div>{p.lead(content)}{content?.summary?.lines.map(line => <p key={line}>{line}</p>)}</div> }))
afterEach(cleanup)

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  qc.setQueryData<SharesData>(shareQueries.ofTicket('demo', 'DEMO-1').queryKey, () => ({ available: true, configured: true, workspace: 'demo', owner: 'demo-owner', live: true, server: 'http://127.0.0.1:8103', share: 'http://127.0.0.1:8104', shares: [], staged: [] }))
  qc.setQueryData(queries.addOns('demo').queryKey, { available: true, add_ons: [{ id: 'publish', title: 'Publish', desc: '', state: 'on', locked: null, blocked: null, suggest: 'http://127.0.0.1:8103', suggest_from: 'computer' }] })
  const staged: Staged = { staged_id: 'demo', ticket: 'DEMO-1', kind: 'secret', days: 7, views: 0, origin: 'person', created_at: null, files: [{ name: 'report.md', size: 10, sha256: 'ab'.repeat(32) }] }
  render(<QueryClientProvider client={qc}><ShareFlow ws="demo" ticket={{ key: 'DEMO-1', artifacts: [] }} only="report.md" resume={staged} onClose={() => {}} /></QueryClientProvider>)
  return qc
}

it('shows the host destination and access once, with the file and link reminder', () => {
  const qc = mount()
  expect(screen.getByText('report.md')).toBeInTheDocument()
  expect(document.body.textContent?.match(/Anyone/gi)).toHaveLength(1)
  expect(document.body.textContent?.match(/7 days/g)).toHaveLength(1)
  expect(document.body.textContent?.match(/files go/gi)).toHaveLength(1)
  expect(screen.getByText('You see the link once.')).toBeVisible()
  qc.clear()
})

it.each([
  { title: 'Share these files', lines: ['An agent prepared this copy.'] },
  { title: '', lines: [] },
])('retains missing facts for summary %j', summary => {
  content = { headline: 'Share', summary }
  const qc = mount()
  expect(screen.getByTestId('where-files-go')).toBeVisible()
  expect(screen.getByRole('note')).toHaveTextContent('Anyone with the link can open it.')
  expect(screen.getByRole('note')).toHaveClass('text-warning')
  expect(screen.getByText('You see the link once.')).toBeVisible()
  qc.clear()
})
