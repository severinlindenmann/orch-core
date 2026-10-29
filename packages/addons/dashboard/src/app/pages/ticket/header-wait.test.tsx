import { expect, it } from 'vitest'
import { terminalWaitFor } from '@/app/attention'
import type { TicketDocument } from '@/api/types'
import { phaseOf } from './progress'

// P04: a terminal wait of the ticket's agent turns the pill from "Getting started" into "Needs you".
const items = [
  { kind: 'terminal_wait', ticket: 'PW-0001', ref: 'run_a' },
  { kind: 'terminal_wait', ticket: 'PW-0002', ref: 'run_b' },
  { kind: 'question', ticket: 'PW-0002', ref: 'Q1' },
]

it('terminalWaitFor picks the run of this ticket only', () => {
  expect(terminalWaitFor(items, 'PW-0002')).toBe('run_b')
  expect(terminalWaitFor(items, 'PW-0003')).toBeUndefined()
  expect(terminalWaitFor([], 'PW-0002')).toBeUndefined()
})

const ticket = { key: 'PW-0002', status: 'open', claim: null } as unknown as TicketDocument
const starting = { state: 'running' as const, started_at: new Date(Date.now() - 5000).toISOString() }

it('the pill reads Needs you while a terminal wait exists, Getting started otherwise', () => {
  const now = Date.now()
  expect(phaseOf(ticket, { needsYou: false, run: starting, now }).kind).toBe('starting')
  expect(phaseOf(ticket, { needsYou: !!terminalWaitFor(items, 'PW-0002'), run: starting, now }).kind).toBe('needs')
})
