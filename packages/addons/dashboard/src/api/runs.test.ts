import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { invalidateLive } from './useLiveUpdates'
import { cleanupDoubt, exitText, mergeRunItems, newIdem, markNoReplay, outputGone, type Run, type RunList } from './runs'

const WS = 'a'.repeat(32)
const run = (id: string, rev: number, over: Partial<Run> = {}): Run => ({
  run: id, rev, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true, state: 'running',
  started_at: '2026-10-14T10:00:00Z', viewers: 0, writer: false, cols: 120, rows: 40, replay_until: null, label: null,
  label_hidden: false, cwd: { kind: 'workspace' }, exit: null, ...over,
})

describe('runs', () => {
  it('live items merge by revision; gone removes; resync reads the full list again', () => {
    const qc = new QueryClient()
    qc.setQueryData<RunList>(['runs', WS], { runs: [run('r1', 5)], runner: null, truncated: false })
    mergeRunItems(qc, WS, [{ t: 'run', run: run('r1', 4, { viewers: 3 }) }]) // older: ignored
    expect(qc.getQueryData<RunList>(['runs', WS])!.runs[0].viewers).toBe(0)
    invalidateLive(qc, WS, 'runs', [{ t: 'run', run: run('r1', 6, { viewers: 2 }) }, { t: 'run', run: run('r2', 7) }] as never)
    expect(qc.getQueryData<RunList>(['runs', WS])!.runs.map((r) => [r.run, r.viewers])).toEqual([['r2', 0], ['r1', 2]])
    mergeRunItems(qc, WS, [{ t: 'gone', run: 'r1' }])
    expect(qc.getQueryData<RunList>(['runs', WS])!.runs.map((r) => r.run)).toEqual(['r2'])
    invalidateLive(qc, WS, 'resync', [])
    expect(qc.getQueryState(['runs', WS])!.isInvalidated).toBe(true)
  })

  it('output gone, cleanup doubt and the idem shape', () => {
    const now = Date.parse('2026-10-14T12:00:00Z')
    expect(outputGone(run('r', 1), now)).toBe(false)
    expect(outputGone(run('r', 1, { state: 'exited', replay_until: '2026-10-14T13:00:00Z' }), now)).toBe(false)
    expect(outputGone(run('r', 1, { state: 'exited', replay_until: '2026-10-14T11:00:00Z' }), now)).toBe(true)
    expect(outputGone(run('r', 1, { state: 'lost', replay_until: null }), now)).toBe(true)
    // an attach that failed once for an ended run is not tried again on later page loads
    const kept = run('rk', 1, { state: 'exited', replay_until: '2026-10-14T13:00:00Z' })
    expect(outputGone(kept, now)).toBe(false)
    markNoReplay('rk')
    expect(outputGone(kept, now)).toBe(true)
    expect(outputGone(run('rk', 1), now)).toBe(false) // a running run is never "gone"
    expect([cleanupDoubt('incomplete'), cleanupDoubt('unknown'), cleanupDoubt('complete'), cleanupDoubt(null)]).toEqual([true, true, false, false])
    for (let i = 0; i < 50; i++) expect(newIdem()).toMatch(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/)
  })
})

describe('M-5: why a run ended', () => {
  const ended = (kind: Run['kind'], exit: Run['exit'], state: Run['state'] = 'exited') => ({ kind, state, exit })
  const ex = (reason: NonNullable<Run['exit']>['reason'], code: number | null, signal: string | null = null) => ({ code, signal, at: '2026-10-14T10:00:00Z', reason, cleanup: 'complete' as const })
  it('says it in plain words, nothing while it runs', () => {
    expect(exitText({ kind: 'agent', state: 'running', exit: null })).toBeNull()
    expect(exitText(ended('agent', ex('spawn_failed', null)))).toBe('The agent could not be started. Is its program installed on this computer?')
    expect(exitText(ended('agent', ex('exited', 127)))).toContain('program was not found')
    expect(exitText(ended('agent', ex('exited', 2)))).toBe('The agent ended with an error (code 2).')
    expect(exitText(ended('shell', ex('exited', 0)))).toBe('The terminal finished.')
    expect(exitText(ended('shell', ex('stopped', null, 'SIGTERM')))).toBe('The terminal was stopped.')
    expect(exitText(ended('agent', ex('runner_restarted', null)))).toContain('runner restarted')
    expect(exitText(ended('agent', null, 'lost'))).toContain('lost')
  })
})
