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
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: false })
  })

  it('a running run without a claim means starting', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - 60_000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: false })
  })

  it('a running run is slow once it passes 90 seconds', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - STARTING_SLOW_MS - 1000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'starting', slow: true })
  })

  it('a running run older than 3 minutes without a claim is idle', () => {
    const run = { state: 'running' as const, started_at: iso(NOW - STARTING_MAX_MS - 1000) }
    expect(phaseOf(ticket(), { needsYou: false, run, now: NOW })).toEqual({ kind: 'idle' })
  })

  it('nothing at all is idle', () => {
    expect(phaseOf(ticket(), { needsYou: false, now: NOW })).toEqual({ kind: 'idle' })
  })
})
