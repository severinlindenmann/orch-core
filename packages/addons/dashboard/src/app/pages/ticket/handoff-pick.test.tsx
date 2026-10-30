import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { HandoffSignDialog } from './HandoffSignDialog'
import type { HumanAction } from './shared'

// The popover renders its children and its confirm label, so the radios can be driven.
vi.mock('@/components/sign/SignPopover', () => ({ SignPopover: (p: { children: ReactNode; confirmLabel: string }) => <div><span data-testid="confirm">{p.confirmLabel}</span>{p.children}</div> }))
vi.mock('./EvidenceView', async (orig) => ({ ...(await orig<typeof import('./EvidenceView')>()), VerdictEvidence: () => null, VerdictPreview: () => null }))
vi.mock('./ManualAcceptance', () => ({ ManualAcceptanceReview: () => null }))
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => vi.fn() }))
vi.mock('./shared', async (orig) => ({ ...(await orig<typeof import('./shared')>()), useViewer: () => ({ name: (x: string) => x }) }))
afterEach(cleanup)
const t = (key: string): TicketDocument =>
  ({ key, gates: { plan: { hash: null }, requirements: { hash: null }, code: { hash: null }, verify: { hash: null } }, questions_state: [], tasks_state: [], artifacts: [], branch: {}, acceptance_state: [{ id: 'AC1', text: 'r', state: 'proven', evidence: [{ kind: 'task', ref: 'T1' }] }] }) as unknown as TicketDocument
const view = (ticket: TicketDocument, action: HumanAction | null) => <QueryClientProvider client={new QueryClient()}><HandoffSignDialog ticket={ticket} action={action} ws="w" onClose={() => {}} /></QueryClientProvider>

it('a pick on ticket A does not carry to ticket B, nor past a close', () => {
  const r = render(view(t('A-1'), { kind: 'verdict' }))
  expect(screen.getByTestId('confirm')).toHaveTextContent('Pass')
  fireEvent.click(screen.getByRole('radio', { name: /Send back/ }))
  expect(screen.getByTestId('confirm')).toHaveTextContent('Send back')
  r.rerender(view(t('B-1'), { kind: 'verdict' }))
  expect(screen.getByTestId('confirm')).toHaveTextContent('Pass')
  fireEvent.click(screen.getByRole('radio', { name: /Send back/ }))
  r.rerender(view(t('B-1'), null))
  r.rerender(view(t('B-1'), { kind: 'verdict' }))
  expect(screen.getByTestId('confirm')).toHaveTextContent('Pass')
})
