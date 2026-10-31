import { describe, expect, it } from 'vitest'
import type { TicketDocument } from '@/api/types'
import { phaseOf, STARTING_MAX_MS, STARTING_SLOW_MS } from './progress'

const ticket = (o: Record<string, unknown> = {}) => ({ key: 'T-1', status: 'in_progress', claim: null, ...o }) as unknown as TicketDocument
const live = { agent: 'a', for: 'u', session: 's', since: '2025-01-01T00:00:00Z' }
const NOW = Date.parse('2025-01-01T01:00:00Z')
const iso = (ms: number) => new Date(ms).toISOString()

describe('phaseOf', () => {
  it('needs you wins over everything', () => {
    const p = phaseOf(ticket({ claim: live }), { needsYou: true, mark: { since: NOW }, now: NOW })
    expect(p).toEqual({ kind: 'needs' })
  })

  it('a mark means starting', () => {
    expect(phaseOf(ticket(), { needsYou: false, mark: { since: NOW - 1000 }, now: NOW })).toEqual({ kind: 'starting', slow: false })
  })

  it('is slow after 90 seconds', () => {
    expect(phaseOf(ticket(), { needsYou: false, mark: { since: NOW - STARTING_SLOW_MS }, now: NOW })).toEqual({ kind: 'starting', slow: true })
  })

  it('a live claim means working', () => {
    expect(phaseOf(ticket({ claim: live }), { needsYou: false, mark: { since: NOW }, now: NOW })).toEqual({ kind: 'working' })
  })

  it('a claim that is not live is not working', () => {
    expect(phaseOf(ticket({ claim: { ...live, live: false } }), { needsYou: false, now: NOW })).toEqual({ kind: 'idle' })
  })

  it('a failed mark means failed', () => {
    const p = phaseOf(ticket(), { needsYou: false, mark: { since: NOW, failed: { reason: 'No agent found' } }, now: NOW })
    expect(p).toMatchObject({ kind: 'failed', reason: 'No agent found' })
  })

  it('a done ticket is idle', () => {
    const p = phaseOf(ticket({ status: 'done', claim: live }), { needsYou: false, mark: { since: NOW }, now: NOW })
    expect(p).toEqual({ kind: 'idle' })
  })

  it('a run that is starting means starting', () => {
    const run = { state: 'starting' as const, started_at: iso(NOW - 2000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: false, live: true })
  })

  it('a running run without a claim means starting', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - 60_000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: false, live: true })
  })

  it('a running run is slow once it passes 90 seconds', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - STARTING_SLOW_MS - 1000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: true, live: true })
  })

  it('a live run without a claim after 4 minutes is never idle and offers no second start (run 16 P01)', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - 4 * 60_000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: true, live: true })
  })

  it('a wait item without a claim gives waiting, with the run to open', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - 4 * 60_000) }
    expect(phaseOf(ticket(), { needsYou: true, run, wait: 'r_1', now: NOW })).toEqual({ kind: 'waiting', run: 'r_1' })
  })

  it('a wait item beside a live claim stays needs', () => {
    expect(phaseOf(ticket({ claim: live }), { needsYou: true, wait: 'r_1', now: NOW })).toEqual({ kind: 'needs' })
  })

  it('a lost run newer than the mark means failed', () => {
    const run = { state: 'lost' as const, started_at: iso(NOW - 1000) }
    const p = phaseOf(ticket(), { needsYou: false, mark: { since: NOW - 5000 }, run, now: NOW })
    expect(p).toEqual({ kind: 'failed', reason: 'The agent stopped before it began. Start it again.' })
  })

  it('an exited run without a mark, started recently, means failed', () => {
    const run = { state: 'exited' as const, started_at: iso(NOW - 60_000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toMatchObject({ kind: 'failed' })
  })

  it('an exited run older than the mark is not a failure', () => {
    const run = { state: 'exited' as const, started_at: iso(NOW - 60_000) }
    expect(phaseOf(ticket(), { needsYou: false, mark: { since: NOW - 1000 }, run, now: NOW })).toEqual({ kind: 'starting', slow: false })
  })

  it('an old exited run without a mark is idle', () => {
    const run = { state: 'exited' as const, started_at: iso(NOW - STARTING_MAX_MS - 1000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'idle' })
  })

  it('a dead run with a live claim is working', () => {
    const run = { state: 'lost' as const, started_at: iso(NOW - 1000) }
    expect(phaseOf(ticket({ claim: live }), { needsYou: false, mark: { since: NOW - 5000 }, run, now: NOW })).toEqual({ kind: 'working' })
  })

  it('a mark older than 3 minutes with no run or claim is idle', () => {
    expect(phaseOf(ticket(), { needsYou: false, mark: { since: NOW - STARTING_MAX_MS }, now: NOW })).toEqual({ kind: 'idle' })
  })

  it('nothing at all is idle', () => {
    expect(phaseOf(ticket(), { needsYou: false, now: NOW })).toEqual({ kind: 'idle' })
  })
})

import { agentFinished } from './progress'
describe('agentFinished (r14 P06)', () => {
  it('is true only for a running agent whose ticket is done or closed', () => {
    expect(agentFinished({ kind: 'agent', state: 'running' }, 'done')).toBe(true)
    expect(agentFinished({ kind: 'agent', state: 'starting' }, 'closed')).toBe(true)
    expect(agentFinished({ kind: 'agent', state: 'running' }, 'open')).toBe(false)
    expect(agentFinished({ kind: 'agent', state: 'running' }, undefined)).toBe(false)
    expect(agentFinished({ kind: 'agent', state: 'exited' }, 'done')).toBe(false)
    expect(agentFinished({ kind: 'shell', state: 'running' }, 'done')).toBe(false)
  })
})
