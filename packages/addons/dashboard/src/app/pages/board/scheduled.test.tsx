import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TicketSummary } from '@/api/types'
import { ScheduledGroup, ScheduledMark } from './scheduled'

const h = vi.hoisted(() => ({ rows: [] as unknown[] }))
vi.mock('@/api/schedules', async (orig) => ({ ...(await orig<typeof import('@/api/schedules')>()), useSchedules: () => ({ data: { rows: h.rows } }) }))
vi.mock('@/api/followups', async (orig) => ({ ...(await orig<typeof import('@/api/followups')>()), followupsOn: () => true }))
vi.mock('@/app/workspace', () => ({ useWorkspace: () => ({ workspace: { id: 'w1', addons: [] } }) }))
vi.mock('./ListView', () => ({ ListView: ({ tickets }: { tickets: TicketSummary[] }) => <ul>{tickets.map((t) => <li key={t.key}>{t.key}</li>)}</ul> }))

const row = (ticket: string, every: string | undefined, state = 'live') => ({ ticket, state, rule: { every, at: '09:00', tz: 'UTC' } })
afterEach(() => { cleanup(); h.rows = [] })

describe('scheduled tickets', () => {
  it('marks a ticket with a live repeating schedule, not a one-time or cancelled one', () => {
    h.rows = [row('T-1', 'day'), row('T-2', undefined), row('T-3', 'week', 'cancelled')]
    render(<><ScheduledMark ticketKey="T-1" /><ScheduledMark ticketKey="T-2" /><ScheduledMark ticketKey="T-3" /></>)
    expect(screen.getAllByTestId('scheduled-mark')).toHaveLength(1)
  })
  it('the group is folded, shows its count, and is absent when empty', () => {
    const t = (key: string) => ({ key, title: key }) as unknown as TicketSummary
    const { container, rerender } = render(<ScheduledGroup tickets={[t('T-1'), t('T-2')]} people={{} as never} onOpen={() => {}} />)
    const d = screen.getByTestId('board-scheduled') as HTMLDetailsElement
    expect(d.open).toBe(false)
    expect(d.querySelector('summary')).toHaveTextContent('Scheduled2')
    rerender(<ScheduledGroup tickets={[]} people={{} as never} onOpen={() => {}} />)
    expect(container.textContent).toBe('')
  })
})
