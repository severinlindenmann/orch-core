import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentsApi, LOGIN_NOTICE, type AgentArgs, type AgentPreview } from '@/api/agents'
import { ApiError } from '@/api/types'
import { EMPTY, handoffNewTicket } from './index'
import { compareStart, fitsWithAccess, MIN_GRANT_MS, shownModel, usableGrant, type Reviewed } from './startAfterCreate'
import { startAfterCreate } from './useStartAfterCreate'

const NOW = Date.parse('2026-10-19T10:00:00Z')
const GRANT = { id: 'gr_A', until: '2026-10-21T10:00:00Z', scope: 'workable' as const }
const args: AgentArgs = { ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true }
const reviewed = (over: Partial<Reviewed> = {}): Reviewed => ({ choice: { harness: 'claude-code', mode: 'work', model: '' }, model: { model: 'opus', source: 'chosen' }, grantId: 'gr_A', ...over })
const preview = (over: Partial<AgentPreview> = {}): AgentPreview => ({
  spec: 's'.repeat(8), ticket: 'DEMO-1', display: 'claude x', display_hidden: false, model: 'opus', model_source: 'chosen',
  cwd: { kind: 'workspace' }, cwd_path: '/w', cwd_path_hidden: false, will_create_worktree: false, login_shell: false, env: [], grant: GRANT, blocked: null, warnings: [], ...over,
})
const ACK = { start_agent: true, login_shell: true }

afterEach(() => vi.restoreAllMocks())

describe('usableGrant', () => {
  it('needs 15 minutes left; unknown or malformed is no grant', () => {
    expect(usableGrant({ grant: { id: 'g', until: new Date(NOW + MIN_GRANT_MS).toISOString(), scope: 'workable' } }, NOW)?.id).toBe('g')
    expect(usableGrant({ grant: { id: 'g', until: new Date(NOW + MIN_GRANT_MS - 1000).toISOString(), scope: 'all' } }, NOW)).toBeNull()
    expect(usableGrant({ grant: { id: 'g', until: 'soon', scope: 'all' } }, NOW)).toBeNull()
    expect(usableGrant({ grant: null }, NOW)).toBeNull()
    expect(usableGrant(null, NOW)).toBeNull()
  })
})

describe('shownModel', () => {
  it('chosen > the agent default (an add-on that chooses answers in the preview only)', () => {
    expect(shownModel({ harness: 'codex', mode: 'work', model: 'o3' })).toEqual({ model: 'o3', source: 'chosen' })
    expect(shownModel({ harness: 'claude-code', mode: 'work', model: '' })).toEqual({ model: null, source: 'default' })
  })
  it('a preview whose model an add-on chose differs from what the page showed', () => {
    const shown = shownModel({ harness: 'claude-code', mode: 'work', model: '' })
    const seen = reviewed({ choice: { harness: 'claude-code', mode: 'work', model: '' }, model: shown })
    expect(compareStart(preview({ model: null, model_source: 'default' }), seen, args, ACK).ok).toBe(true)
    expect(compareStart(preview({ model: 'opus', model_source: 'addon', model_by: 'Model routing' }), seen, args, ACK).ok).toBe(false)
  })
})

describe('compareStart (fail closed)', () => {
  it('accepts exactly what was shown', () => expect(compareStart(preview(), reviewed(), args, ACK)).toEqual({ ok: true }))
  it.each([
    ['blocked', preview({ blocked: { code: 'x', message: 'nope', detail: {} } })],
    ['no spec', preview({ spec: null })],
    ['model', preview({ model: 'sonnet' })],
    ['model source', preview({ model_source: 'addon' })],
    ['worktree', preview({ will_create_worktree: true })],
    ['a repository\'s shared copy', preview({ cwd: { kind: 'repo' } })],
    ['a worktree folder', preview({ cwd: { kind: 'worktree' } })],
    ['hidden folder', preview({ cwd_path: null })],
    ['hidden command', preview({ display_hidden: true })],
    ['no grant', preview({ grant: null })],
    ['other grant', preview({ grant: { ...GRANT, id: 'gr_B' } })],
    ['unknown warning', preview({ warnings: ['something new'] })],
  ])('refuses %s', (_n, p) => expect(compareStart(p, reviewed(), args, ACK).ok).toBe(false))
  it("accepts the ticket's own work folder (a ticket without a codebase), and no other ticket's", () => {
    const own = preview({ cwd: { kind: 'workdir', ticket: args.ticket }, workdir: { exists: false, empty: true } })
    expect(compareStart(own, reviewed(), args, ACK)).toEqual({ ok: true })
    expect(compareStart(preview({ cwd: { kind: 'workdir', ticket: 'DEMO-9999' }, workdir: { exists: false, empty: true } }), reviewed(), args, ACK).ok).toBe(false)
    expect(compareStart(preview({ cwd: { kind: 'workdir' } }), reviewed(), args, ACK).ok).toBe(false)
  })
  it('refuses a work folder that already holds files (a committed .claude/settings.json), or one the host did not describe', () => {
    const at = (workdir?: { exists: boolean; empty: boolean }) => preview({ cwd: { kind: 'workdir', ticket: args.ticket }, workdir })
    expect(compareStart(at({ exists: true, empty: false }), reviewed(), args, ACK).ok).toBe(false)
    expect(compareStart(at(undefined), reviewed(), args, ACK).ok).toBe(false)
    expect(compareStart(at({ exists: true, empty: true }), reviewed(), args, ACK)).toEqual({ ok: true })
  })
  it('refuses another agent or mode than the choice', () => {
    expect(compareStart(preview(), reviewed(), { ...args, harness: 'codex' }, ACK).ok).toBe(false)
    expect(compareStart(preview(), reviewed(), { ...args, mode: 'fix' }, ACK).ok).toBe(false)
  })
  it('refuses until the notices are acknowledged', () => {
    expect(compareStart(preview(), reviewed(), args, { start_agent: false, login_shell: true }).ok).toBe(false)
    expect(compareStart(preview({ warnings: [LOGIN_NOTICE] }), reviewed(), args, { start_agent: true, login_shell: false }).ok).toBe(false)
    expect(compareStart(preview({ warnings: [LOGIN_NOTICE] }), reviewed(), args, ACK).ok).toBe(true)
  })
  it('a grant signed with the ticket must be a new one, not the old one', () => {
    const fresh = reviewed({ grantId: null })
    expect(compareStart(preview(), fresh, args, ACK, ['gr_OLD']).ok).toBe(true)
    expect(compareStart(preview({ grant: { ...GRANT, id: 'gr_OLD' } }), fresh, args, ACK, ['gr_OLD']).ok).toBe(false)
  })
})

describe('step limit', () => {
  const sections = { summary: 'a', context: 'b', requirements: 'c', out_of_scope: 'd', plan: 'e', decisions: 'f' }
  const draft = { ...EMPTY, title: 'T', priority: 'high' as const, sections, acceptance: ['1', '2', '3', '4', '5', '6', '7'] } // 1 + 1 + 6 + 7 = 15
  it('counts the access as one more signature', () => {
    expect(handoffNewTicket(draft, undefined, null)).toHaveProperty('action')
    expect(handoffNewTicket(draft, undefined, 24)).toMatchObject({ action: { agent: { hours: 24 } } })
    expect(handoffNewTicket({ ...draft, acceptance: [...draft.acceptance, '8'] }, undefined, null)).toHaveProperty('action') // 16: fits
    expect(handoffNewTicket({ ...draft, acceptance: [...draft.acceptance, '8'] }, undefined, 24)).toMatchObject({ tooMany: true })
    expect(fitsWithAccess(15, true)).toBe(true)
    expect(fitsWithAccess(16, true)).toBe(false)
    expect(fitsWithAccess(16, false)).toBe(true)
  })
  it('sends no agent key at all without access (the intent stays as it was)', () => {
    expect(JSON.stringify(handoffNewTicket(draft, undefined, null))).not.toContain('agent')
  })
})

describe('startAfterCreate', () => {
  const req = { ws: 'w', key: 'DEMO-1', reviewed: reviewed(), idem: 'idem-1', priorGrantIds: [] as string[] }
  const spies = (p = preview()) => ({
    preview: vi.spyOn(agentsApi, 'preview').mockResolvedValue(p),
    notices: vi.spyOn(agentsApi, 'riskNotices').mockResolvedValue({ acknowledged: { start_agent: true } }),
    start: vi.spyOn(agentsApi, 'start').mockResolvedValue({ run: { run: 'r_1' } as never }),
  })
  it('previews the own key once, then starts once with the spec and the idem', async () => {
    const s = spies()
    const out = await startAfterCreate(req)
    expect(out.kind).toBe('started')
    expect(s.preview).toHaveBeenCalledTimes(1)
    expect(s.preview).toHaveBeenCalledWith('w', { ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true })
    expect(s.start).toHaveBeenCalledWith('w', { ticket: 'DEMO-1', harness: 'claude-code', mode: 'work', worktree: true }, 'ssssssss', 'idem-1')
  })
  it('sends a chosen model with both calls', async () => {
    const s = spies(preview({ model: 'sonnet', model_source: 'chosen' }))
    await startAfterCreate({ ...req, reviewed: reviewed({ choice: { harness: 'claude-code', mode: 'work', model: 'sonnet' }, model: { model: 'sonnet', source: 'chosen' } }) })
    expect(s.start.mock.calls[0][1]).toMatchObject({ model: 'sonnet' })
  })
  it('asks (never starts) on a mismatch, with the reason in plain words', async () => {
    const s = spies(preview({ model: 'sonnet' }))
    const out = await startAfterCreate(req)
    expect(out).toMatchObject({ kind: 'ask', reason: expect.stringContaining('Created DEMO-1. The agent was not started: the model differs') })
    expect(s.start).not.toHaveBeenCalled()
  })
  it('one failed start is not retried; a refusal gets no old idem, a lost answer keeps it', async () => {
    const s = spies()
    s.start.mockRejectedValueOnce(new ApiError(409, { code: 'launch.changed', message: 'runner.stale: the runner restarted', retryable: false }))
    const refused = await startAfterCreate(req)
    expect(refused).toMatchObject({ kind: 'ask', initial: { note: expect.stringContaining('runner.stale: the runner restarted') } })
    expect((refused as { initial: { idem?: string } }).initial.idem).toBeUndefined()
    s.start.mockRejectedValueOnce(new ApiError(503, { code: 'host.unavailable', message: 'down', retryable: true }))
    const lost = await startAfterCreate(req)
    expect((lost as { initial: { idem?: string } }).initial.idem).toBe('idem-1')
    expect(s.start).toHaveBeenCalledTimes(2)
    expect(s.preview).toHaveBeenCalledTimes(2) // one preview per attempt, none inside
  })
  it('unreadable notices count as not acknowledged', async () => {
    const s = spies()
    s.notices.mockRejectedValue(new Error('x'))
    expect((await startAfterCreate(req)).kind).toBe('ask')
    expect(s.start).not.toHaveBeenCalled()
  })
})
