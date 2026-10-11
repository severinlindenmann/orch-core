import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { offered } from '@/test/offered'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import type { LaunchPreview } from '@/api/types'
import { describeEvent } from '@/mocks/derive'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'
import { getAddon } from './registry'

// model routing (`models`, capability `launch`): picks the model a start-agent session starts on.
const setup = (viewer = 'p_sev', install = true) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (install) installAndGrant(store, ws, 'models')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
const preview = async (s: S, key = 'DEMO-0044') => ((await s.api.getAddonState(s.ws, 'start-agent', key)) as unknown as { previews: Record<string, LaunchPreview> }).previews[key]
const models = async (s: S) => (await s.api.getAddonState(s.ws, 'models')) as unknown as { settings: Record<string, string>; settingsAlert: { type: string; tone?: string; title?: string; text?: string }; nextStrong: string[] }
const choose = (s: S, key: string, formData: Record<string, string>) => s.api.runAddonAction(s.ws, 'start-agent', 'configure', { ticket: key, formData })
const save = (s: S, formData: Record<string, string>) => s.api.runAddonAction(s.ws, 'models', 'save_settings', { formData })
/** Start as core does after its dialog: the ticket and the choice the preview asks for. */
const startOn = async (s: S, key: string) => s.api.runAddonAction(s.ws, 'start-agent', 'start', { ticket: key, confirmed: true, launch: (await preview(s, key)).request })
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string; message: string }) => `${e.status} ${e.code}: ${e.message}`)
const QUESTION = 'T2 failed its check in two sessions: start the next session on Strong?'

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('model routing package', () => {
  it('starts in the catalog with the launch capability; settings are owner-only, escalate maintainer', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.models).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'models')!
    expect(pkg.capabilities).toEqual(['launch'])
    expect(pkg.actions).toMatchObject({ save_settings: { minRole: 'owner' }, escalate: { minRole: 'maintainer' } })
  })
  it('off: the preview has no model line and no --model', async () => {
    const p = await preview(setup('p_sev', false))
    expect(p.model).toBeUndefined()
    expect(p.command).not.toContain('--model')
  })
})

describe('the start-agent preview with model routing on', () => {
  it('work runs on Standard: the model line, --model sonnet and the subagent model in the environment', async () => {
    const p = await preview(setup())
    expect(p.model).toBe('Model: Standard (sonnet) · subagents on haiku')
    expect(p.command).toBe("CLAUDE_CODE_SUBAGENT_MODEL=haiku orch session start --in background DEMO-0044 -- claude --model sonnet '/orch:work DEMO-0044'")
  })
  it('refine runs on Strong; a tier set to none uses the harness default', async () => {
    const s = setup()
    await choose(s, 'DEMO-0044', { mode: 'refine' })
    expect((await preview(s)).model).toBe('Model: Strong (opus) · subagents on haiku')
    await save(s, { ...(await models(s)).settings, fix: 'none' })
    await choose(s, 'DEMO-0044', { mode: 'fix' })
    const p = await preview(s)
    expect(p.model).toBe('Model: the harness default · subagents on haiku')
    expect(p.command).not.toContain('--model')
  })
  it('light without a light model uses Standard; continue runs on the tier of the last start (Standard when none)', async () => {
    const s = setup()
    await save(s, { ...(await models(s)).settings, light: '', work: 'light' })
    expect((await preview(s)).model).toBe('Model: Standard (sonnet), as Light has no model · subagents on haiku')
    await choose(s, 'DEMO-0044', { mode: 'continue' })
    expect((await preview(s)).model).toBe('Model: Standard (sonnet) · subagents on haiku')
    await choose(s, 'DEMO-0048', { mode: 'refine' })
    await startOn(s, 'DEMO-0048')
    await s.api.runAddonAction(s.ws, 'start-agent', 'stop', { ticket: 'DEMO-0048' })
    await choose(s, 'DEMO-0048', { mode: 'continue' })
    expect((await preview(s, 'DEMO-0048')).model).toBe('Model: Strong (opus) · subagents on haiku')
  })
  it('Codex is not routed: it starts on its own default', async () => {
    const s = setup()
    await choose(s, 'DEMO-0044', { harness: 'codex' })
    const p = await preview(s)
    expect(p.model).toBe("Model: Codex's own default (model routing covers Claude Code)")
    expect(p.command).not.toContain('--model')
    expect(p.command).not.toContain('CLAUDE_CODE_SUBAGENT_MODEL')
  })
  it('the session records the model it started on', async () => {
    const s = setup()
    await startOn(s, 'DEMO-0044')
    const ev = s.store.wsEventsOf(s.ws).find((e) => e.type === 'agent.started')!
    expect(ev).toMatchObject({ model: 'sonnet', tier: 'standard' })
    expect(String(ev.command)).toContain('--model sonnet')
    expect((await s.api.getAgents(s.ws)).find((a) => a.session === ev.session)!.model).toBe('sonnet')
  })
})

describe('settings that are not model names', () => {
  const SENTENCE = 'The Standard model "son net" is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.'
  it('show a warning and block Start with a sentence', async () => {
    const s = setup()
    await save(s, { ...(await models(s)).settings, standard: 'son net' })
    expect((await models(s)).settingsAlert).toMatchObject({ type: 'alert', tone: 'warn', title: 'Standard is not a model name' })
    expect((await preview(s)).blocked).toBe(SENTENCE)
    expect(await fail(startOn(s, 'DEMO-0044'))).toBe(`409 launch.invalid_model: ${SENTENCE}`)
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'agent.started')).toBe(false)
  })
  it('a leading "-" is refused too (it would be read as a flag); a valid alias with brackets is fine', async () => {
    const s = setup()
    await save(s, { ...(await models(s)).settings, subagent: '--dangerously' })
    expect((await preview(s)).blocked).toMatch(/^The subagent model "--dangerously" is not a model name/)
    await save(s, { ...(await models(s)).settings, subagent: 'haiku', strong: 'sonnet[1m]' })
    expect((await preview(s)).blocked).toBeUndefined()
    expect((await models(s)).settingsAlert.type).toBe('stack')
  })
  it('shell or flag text in a model name is refused with the sentence (the host checks again)', async () => {
    const s = setup()
    for (const bad of ['sonnet; rm -rf ~', '$(id)', '-x', 'a b', 'son\nnet']) {
      await save(s, { ...(await models(s)).settings, standard: bad })
      expect((await preview(s)).blocked, JSON.stringify(bad)).toBe(`The Standard model "${bad}" is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.`)
      expect(await fail(startOn(s, 'DEMO-0044'))).toMatch(/^409 launch.invalid_model/)
    }
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'agent.started')).toBe(false)
  })
  it('core refuses a bad model from a launch addon even if the addon did not check it', async () => {
    const s = setup()
    const mod = getAddon('models')!
    vi.spyOn(mod, 'launch').mockReturnValue({ model: 'sonnet; rm -rf ~', subagentModel: 'haiku', line: 'Model · fine' })
    const p = await preview(s)
    expect(p.blocked).toBe('The model "sonnet; rm -rf ~" from Model routing (models) is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.')
    expect(p.command).not.toContain('rm -rf')
    expect(await fail(startOn(s, 'DEMO-0044'))).toMatch(/^409 launch.invalid_model/)
    vi.restoreAllMocks()
  })
  it('a valid name gives the expected argv and env; the display string quotes it', () => {
    const s = setup()
    s.store.runAddon(s.ws, 'models', 'save_settings', { formData: { ...(s.store.addonStateView(s.ws, 'models')!.settings as object), standard: 'sonnet[1m]' } })
    const { spec, command } = s.store.resolveLaunch(s.ws, { ticket: 'DEMO-0044', mode: 'work', harness: 'claude-code', where: 'background' })
    expect(spec).toEqual({ argv: ['orch', 'session', 'start', '--in', 'background', 'DEMO-0044', '--', 'claude', '--model', 'sonnet[1m]', '/orch:work DEMO-0044'], env: { CLAUDE_CODE_SUBAGENT_MODEL: 'haiku' } })
    expect(command).toBe("CLAUDE_CODE_SUBAGENT_MODEL=haiku orch session start --in background DEMO-0044 -- claude --model 'sonnet[1m]' '/orch:work DEMO-0044'")
  })
  it('a plan that blocks only when the start commits is still refused, before anything is recorded', async () => {
    const s = setup()
    const mod = getAddon('models')!
    const real = mod.launch!.bind(mod)
    vi.spyOn(mod, 'launch').mockImplementation((st, req, c) => (c.commit ? { error: 'Blocked at commit.' } : real(st, req, c)))
    expect((await preview(s)).blocked).toBeUndefined()
    expect(await fail(startOn(s, 'DEMO-0044'))).toBe('409 launch.invalid_model: Blocked at commit.')
    expect(s.store.wsEventsOf(s.ws).some((e) => e.type === 'agent.started')).toBe(false)
  })
  it('the bad-model sentence from core names the addon that supplied it', async () => {
    const s = setup()
    vi.spyOn(getAddon('models')!, 'launch').mockReturnValue({ model: '$(id)' })
    expect((await preview(s)).blocked).toBe('The model "$(id)" from Model routing (models) is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.')
  })
  it('a launch hook\'s free-text line and an unknown tier never become core facts', async () => {
    const s = setup()
    vi.spyOn(getAddon('models')!, 'launch').mockReturnValue({ model: 'sonnet', tier: 'root', subagentModel: 'haiku', line: 'Approved by owner' })
    const core = await s.api.previewLaunch(s.ws, { ticket: 'DEMO-0044', mode: 'work', harness: 'claude-code', where: 'background' })
    expect(core).toMatchObject({ model: 'sonnet', subagent_model: 'haiku', line: 'Approved by owner', line_by: 'models' })
    expect(core.tier).toBeUndefined()
    await startOn(s, 'DEMO-0044')
    const ev = s.store.wsEventsOf(s.ws).find((e) => e.type === 'agent.started')!
    expect(ev.tier).toBeUndefined()
    expect(ev.model).toBe('sonnet')
  })
  it('only an owner saves settings', async () => {
    const s = setup('p_mara')
    expect(await fail(save(s, { standard: 'opus' }))).toMatch(/^403 forbidden/)
  })
})

describe('escalation after a task failed its check in two sessions', () => {
  it('DEMO-0045 is seeded with T2 failing in two sessions and T2 still open', () => {
    const { store } = setup()
    const runs = store.eventsOf('DEMO-0045').filter((e) => e.type === 'task.run' && e.task === 'T2')
    expect(runs.map((e) => (e.actor as { session: string }).session)).toEqual(['s_9e3f', 's_a41'])
    expect(store.ticket('DEMO-0045')!.tasks_state.find((t) => t.id === 'T2')!.state).toBe('todo')
    expect(describeEvent(runs[0])).toBe('ran the check of T2: failed (exit 1)')
  })
  it('Today gets a core-rendered decision with the last log lines', async () => {
    const s = setup()
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'models')!
    expect(d).toMatchObject({ ticket: 'DEMO-0045', question: QUESTION, action: 'escalate', title: 'Model routing' })
    expect(d.options.map((o) => o.label)).toEqual(['Next start on Strong', 'Not now'])
    expect(d.detail).toContain('Got 24 rows masked for 2026-10-03, expected 6')
  })
  it('without model routing there is no decision', async () => {
    const s = setup('p_sev', false)
    expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.addon === 'models')).toBe(false)
  })
  it('"Next start on Strong" makes the next start on DEMO-0045 run on Strong, once', async () => {
    const s = setup()
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'models')!
    await s.api.runAddonAction(s.ws, 'models', 'escalate', offered(s.store, s.ws, 'models', 'escalate', { id: d.id, confirmed: true, option: 'strong', ticket: 'DEMO-0045' }))
    expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.addon === 'models')).toBe(false)
    expect((await models(s)).nextStrong).toEqual(['DEMO-0045'])
    expect((await preview(s, 'DEMO-0045')).model).toBe('Model: Strong (opus), after failed checks · subagents on haiku')
    await startOn(s, 'DEMO-0045')
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'agent.started')).toMatchObject({ model: 'opus', tier: 'strong' })
    expect((await models(s)).nextStrong).toEqual([])
    await s.api.runAddonAction(s.ws, 'start-agent', 'stop', { ticket: 'DEMO-0045' })
    expect((await preview(s, 'DEMO-0045')).model).toBe('Model: Standard (sonnet) · subagents on haiku')
  })
  it('"Not now" hides the card until another session fails the task', async () => {
    const s = setup()
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'models')!
    await s.api.runAddonAction(s.ws, 'models', 'escalate', offered(s.store, s.ws, 'models', 'escalate', { id: d.id, confirmed: true, option: 'not_now', ticket: 'DEMO-0045' }))
    expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.addon === 'models')).toBe(false)
    s.store.append('DEMO-0045', { type: 'task.run', actor: 'claude-code:s_c0de:p_sev', task: 'T2', receipt: { exit: 1, ms: 50_000 } })
    expect((await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'models')!.question).toBe('T2 failed its check in 3 sessions: start the next session on Strong?')
  })
  it('a closed or made-up decision does nothing', async () => {
    const s = setup()
    const res = await refused(s.api.runAddonAction(s.ws, 'models', 'escalate', offered(s.store, s.ws, 'models', 'escalate', { id: 'models.escalate:DEMO-0044:T1:2', confirmed: true, option: 'strong', ticket: 'DEMO-0044' })))
    expect(res).toMatchObject({ status: 409, code: 'decision.closed', message: 'That decision is closed.' })
    expect((await models(s)).nextStrong).toEqual([])
  })
  it('people who cannot see DEMO-0045 get no decision and cannot escalate it', async () => {
    const s = setup('p_mara')
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0045')!.visibility = { restricted: ['p_sev'] }
    expect((await s.api.getAddonDecisions(s.ws)).some((x) => x.addon === 'models')).toBe(false)
    expect(await fail(s.api.runAddonAction(s.ws, 'models', 'escalate', offered(s.store, s.ws, 'models', 'escalate', { id: 'models.escalate:DEMO-0045:T2:2', confirmed: true, option: 'strong', ticket: 'DEMO-0045' })))).toMatch(/^404/)
    expect(JSON.stringify(await models(s))).not.toContain('DEMO-0045')
  })
})
