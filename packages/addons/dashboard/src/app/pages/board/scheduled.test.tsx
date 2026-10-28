import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScheduleRow } from '@/api/schedules'
import type { TicketSummary } from '@/api/types'
import { ScheduledGroup, ScheduledKeysProvider, ScheduledMark, scheduledKeysOf, splitScheduled } from './scheduled'

vi.mock('./ListView', () => ({ ListView: ({ tickets }: { tickets: TicketSummary[] }) => <ul>{tickets.map((t) => <li key={t.key}>{t.key}</li>)}</ul> }))

const row = (ticket: string, every: string | undefined, state = 'live') => ({ ticket, state, rule: { every, at: '09:00', tz: 'UTC' } }) as unknown as ScheduleRow
const t = (key: string, status = 'open', type = 'task') => ({ key, title: key, status, type }) as unknown as TicketSummary
afterEach(() => cleanup())

describe('scheduledKeysOf', () => {
  it('keeps live repeating schedules only', () => {
    const keys = scheduledKeysOf([row('T-1', 'day'), row('T-2', undefined), row('T-3', 'week', 'cancelled')], true)
    expect([...keys]).toEqual(['T-1'])
  })
  it('is empty when the schedules add-on is off', () => {
    expect(scheduledKeysOf([row('T-1', 'day')], false).size).toBe(0)
  })
})

describe('splitScheduled', () => {
  const keys = new Set(['T-1', 'T-2', 'E-1'])
  it('moves only Open tickets; one in progress stays put', () => {
    const { scheduled, rest } = splitScheduled([t('T-1'), t('T-2', 'in_progress'), t('T-3')], keys, false)
    expect(scheduled.map((x) => x.key)).toEqual(['T-1'])
    expect(rest.map((x) => x.key)).toEqual(['T-2', 'T-3'])
  })
  it('changes nothing for empty keys', () => {
    const all = [t('T-1')]
    const r = splitScheduled(all, new Set(), false)
    expect(r.rest).toBe(all)
    expect(r.scheduled).toEqual([])
  })
  it('an epic that is a lane header is never also a Scheduled row; a flat board has it as a card, so it moves', () => {
    expect(splitScheduled([t('E-1', 'open', 'epic')], keys, true).scheduled).toEqual([])
    expect(splitScheduled([t('E-1', 'open', 'epic')], keys, false).scheduled.map((x) => x.key)).toEqual(['E-1'])
  })
})

describe('scheduled UI', () => {
  it('the clock shows on a ticket whose key the board gave', () => {
    render(<ScheduledKeysProvider value={new Set(['T-1'])}><ScheduledMark ticketKey="T-1" /><ScheduledMark ticketKey="T-2" /></ScheduledKeysProvider>)
    expect(screen.getAllByTestId('scheduled-mark')).toHaveLength(1)
  })
  it('the group is folded, shows its count, and is absent when empty', () => {
    const { container, rerender } = render(<ScheduledGroup tickets={[t('T-1'), t('T-2')]} people={{} as never} onOpen={() => {}} />)
    const d = screen.getByTestId('board-scheduled') as HTMLDetailsElement
    expect(d.open).toBe(false)
    expect(d.querySelector('summary')).toHaveTextContent('Scheduled2')
    rerender(<ScheduledGroup tickets={[]} people={{} as never} onOpen={() => {}} />)
    expect(container.textContent).toBe('')
  })
})
