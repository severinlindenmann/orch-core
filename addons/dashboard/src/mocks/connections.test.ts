import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { parseSecretsFile } from '@/api/secrets'
import { ApiError } from '@/api/types'
import connectionsFixture from './fixtures/connections.json'
import { createMockStore } from './store'

const DEMO = '6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11'
const INT = '0a5e7c31-2d9f-4b68-8e1a-5c3f7d9b2e44'

function setup(viewer = 'p_sev') {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api }
}
const refusal = async (p: Promise<unknown>) => {
  try {
    await p
  } catch (e) {
    if (e instanceof ApiError) return { status: e.status, code: e.code, message: e.message }
    throw e
  }
  throw new Error('expected a refusal')
}
/** Every secret value in the seeded file, read straight from the host fixture. */
const VALUES = parseSecretsFile(connectionsFixture.DEMO.secrets.text).entries.map((e) => e.value)

describe('skills', () => {
  it('lists built-in and workspace skills with their sidecar status', async () => {
    const { api } = setup()
    const skills = await api.getSkills(DEMO)
    const by = Object.fromEntries(skills.map((s) => [s.name, s]))
    expect(by.orch.scope).toBe('built_in')
    expect(by['github-prs'].scope).toBe('built_in') // the github addon is active in DEMO
    expect(by['dbt-seeds']).toMatchObject({ scope: 'workspace', needs: 'declared', version: '1.2.0' })
    expect(by['meter-notes']).toMatchObject({ needs: 'unknown', sidecar: null, version: null })
    // SKILL.md frontmatter carries name and description only.
    expect(by['dbt-seeds'].skill_md).toMatch(/^---\nname: dbt-seeds\ndescription: .+\n---\n/)
    expect(by['tariff-feed'].env).toEqual([
      { name: 'TARIFF_API_TOKEN', granted: true, in_secrets_file: true },
      { name: 'TARIFF_WEBHOOK_SECRET', granted: false, in_secrets_file: false },
    ])
  })
  it('a workspace without config has the built-in skills only (no github-prs without the addon)', async () => {
    const { api } = setup()
    const names = (await api.getSkills(INT)).map((s) => s.name)
    expect(names).toContain('orch')
    expect(names.every((n) => ['orch', 'orch-evidence', 'github-prs'].includes(n))).toBe(true)
  })
  it('lists only tickets the viewer can see', async () => {
    // DEMO-0044 is restricted to Severin and Mara; Tom (viewer) may not see it.
    expect(setup().store.ticket('DEMO-0044')!.visibility).toEqual({ restricted: ['p_sev', 'p_mara'] })
    const t = async (who: string) => (await setup(who).api.getSkills(DEMO)).find((s) => s.name === 'warehouse-admin')!.tickets
    const c = async (who: string) => (await setup(who).api.getConnections(DEMO)).find((s) => s.name === 'az-storage')!.tickets
    expect(await t('p_sev')).toEqual(['DEMO-0044'])
    expect(await t('p_tom')).toEqual([])
    expect(await c('p_mara')).toEqual(['DEMO-0044'])
    expect(await c('p_tom')).toEqual([])
  })
  it('a sidecar that does not validate gives "invalid" needs, uses nothing and refuses grants', async () => {
    const { api } = setup()
    const s = (await api.getSkills(DEMO)).find((x) => x.name === 'naming-conventions')!
    expect(s).toMatchObject({ needs: 'invalid', version: null, connections: [], env: [] })
    expect(s.sidecar_problem).toMatch(/skill_version|secrets/)
    expect(await refusal(api.grantSkillCredentials(DEMO, 'naming-conventions', { connections: ['gh'], env: [], confirmed: true }))).toMatchObject({ status: 409, code: 'skill.invalid_sidecar' })
  })
})

describe('credential grants', () => {
  it('the owner grants an env name; the audit event is recorded with Touch ID presence', async () => {
    const { api, store } = setup()
    const skill = await api.grantSkillCredentials(DEMO, 'tariff-feed', { connections: [], env: ['TARIFF_WEBHOOK_SECRET'], confirmed: true })
    expect(skill.env.find((e) => e.name === 'TARIFF_WEBHOOK_SECRET')?.granted).toBe(true)
    const ev = store.wsEventsOf(DEMO).at(-1)!
    expect(ev).toMatchObject({ type: 'skill.credentials_granted', skill: 'tariff-feed', env: ['TARIFF_WEBHOOK_SECRET'], connections: [], presence: 'touchid', actor: { kind: 'person', id: 'p_sev' } })
    expect(skill.grants.at(-1)).toMatchObject({ by: 'p_sev', env: ['TARIFF_WEBHOOK_SECRET'] })
  })
  it('adding a connection reference writes it into the sidecar, and a skill without one gets one', async () => {
    const { api } = setup()
    const skill = await api.grantSkillCredentials(DEMO, 'meter-notes', { connections: ['databricks-prod'], env: [], confirmed: true })
    expect(skill.needs).toBe('declared')
    expect(JSON.parse(skill.sidecar!)).toMatchObject({ schema_version: 1, scope: 'workspace', connections: ['databricks-prod'], env: [] })
  })
  it('refuses without the signature, from a maintainer, for built-in skills, unknown names and repeats', async () => {
    const { api } = setup()
    expect(await refusal(api.grantSkillCredentials(DEMO, 'dbt-seeds', { connections: [], env: ['X_TOKEN'], confirmed: false as unknown as true }))).toMatchObject({ status: 409, code: 'confirm.required' })
    expect(await refusal(api.grantSkillCredentials(DEMO, 'orch', { connections: ['gh'], env: [], confirmed: true }))).toMatchObject({ status: 409, code: 'skill.built_in' })
    expect(await refusal(api.grantSkillCredentials(DEMO, 'dbt-seeds', { connections: ['nope'], env: [], confirmed: true }))).toMatchObject({ status: 400, code: 'validation.connection' })
    expect(await refusal(api.grantSkillCredentials(DEMO, 'dbt-seeds', { connections: [], env: ['lower_case'], confirmed: true }))).toMatchObject({ status: 400, code: 'validation.env' })
    expect(await refusal(api.grantSkillCredentials(DEMO, 'dbt-seeds', { connections: ['gh'], env: [], confirmed: true }))).toMatchObject({ status: 409, code: 'skill.already_granted' })
    const mara = setup('p_mara')
    expect(await refusal(mara.api.grantSkillCredentials(DEMO, 'tariff-feed', { connections: [], env: ['TARIFF_WEBHOOK_SECRET'], confirmed: true }))).toMatchObject({ status: 403, code: 'forbidden' })
  })
})

describe('connections and checks', () => {
  it('seeds each check state with expected and actual for wrong identity', async () => {
    const { api } = setup()
    const by = Object.fromEntries((await api.getConnections(DEMO)).map((c) => [c.name, c]))
    expect(by.gh.last_check?.status).toBe('ok')
    expect(by['databricks-prod']).toMatchObject({ kind: 'cli_login', login_hint: expect.stringMatching(/^databricks auth login/), last_check: { status: 'auth_expired' } })
    expect(by['gcloud-billing'].last_check).toMatchObject({ status: 'wrong_identity', expected: 'orch-agent@acme-energy.iam.gserviceaccount.com', actual: 'severin@acme-energy.ch' })
    expect(by['tariff-api']).toMatchObject({ kind: 'api_token', login_hint: null, env: ['TARIFF_API_TOKEN'], last_check: { status: 'service_down' } })
    expect(by['az-storage'].last_check?.status).toBe('unknown')
    expect(by['databricks-prod'].tickets).toEqual(['DEMO-0053'])
  })
  it('a check runs with a timeout and bounded output, filtered for secret values', async () => {
    const { api, store } = setup()
    const c = await api.runConnectionCheck(DEMO, 'databricks-ci')
    expect(c.last_check).toMatchObject({ status: 'ok', trigger: 'on_demand', timeout_s: 10 })
    expect(c.last_check!.output.length).toBeLessThanOrEqual(12)
    expect(c.last_check!.output).toContain('> * Authorization: Bearer •••• (DATABRICKS_TOKEN)')
    expect(store.wsEventsOf(DEMO).at(-1)).toMatchObject({ type: 'connection.checked', name: 'databricks-ci', actor: { kind: 'host' } })
  })
  it.each(['p_sev', 'p_mara'])('no answer and no recorded event ever carries a secret value (%s)', async (who) => {
    const { api, store } = setup(who)
    const doctor = await api.runDoctor(DEMO)
    await api.runConnectionCheck(DEMO, 'tariff-api')
    await api.runConnectionCheck(DEMO, 'databricks-ci')
    const answers = JSON.stringify([
      doctor,
      await api.getSkills(DEMO),
      await api.getConnections(DEMO),
      await api.getSecretsFile(DEMO),
      await api.getTicket('DEMO-0043'),
      await api.getAddonState(DEMO, 'terminals'),
      await api.getAddonState(DEMO, 'terminals', 'DEMO-0043'),
      store.wsEventsOf(DEMO),
    ])
    // Every parsed value, and the value on the refused `export` line.
    const all = [...VALUES, 'https://hooks.slack.example/T000/B000/abcdefgh']
    expect(VALUES.length).toBeGreaterThan(3)
    for (const v of all) expect(answers).not.toContain(v)
  })
  it('viewers cannot run the doctor', async () => {
    expect(await refusal(setup('p_tom').api.runDoctor(DEMO))).toMatchObject({ status: 403, code: 'forbidden' })
  })
  it('a check from the re-login item is marked as such (the demo assumes the owner logged in)', async () => {
    const c = await setup().api.runConnectionCheck(DEMO, 'gcloud-billing', 'relogin')
    expect(c.last_check).toMatchObject({ status: 'ok', trigger: 'relogin' })
  })
  it('viewers cannot run checks; re-login is the owner\'s and only for CLI logins', async () => {
    expect(await refusal(setup('p_tom').api.runConnectionCheck(DEMO, 'gh'))).toMatchObject({ status: 403 })
    expect(await refusal(setup('p_mara').api.runConnectionCheck(DEMO, 'databricks-prod', 'relogin'))).toMatchObject({ status: 403 })
    expect(await refusal(setup().api.runConnectionCheck(DEMO, 'tariff-api', 'relogin'))).toMatchObject({ status: 409, code: 'connection.no_login' })
    expect(await setup('p_mara').api.runConnectionCheck(DEMO, 'gh')).toMatchObject({ name: 'gh' })
  })
  it('after the owner logs in again, the check is ok and the ticket is no longer blocked', async () => {
    const { api } = setup()
    expect((await api.getTicket('DEMO-0053')).needs?.blocked).toEqual({ connection: 'databricks-prod', status: 'auth_expired' })
    expect((await api.runConnectionCheck(DEMO, 'databricks-prod')).last_check?.status).toBe('auth_expired') // nothing changed yet
    expect((await api.runConnectionCheck(DEMO, 'databricks-prod', 'relogin')).last_check?.status).toBe('ok')
    expect((await api.runConnectionCheck(DEMO, 'databricks-prod')).last_check?.status).toBe('ok')
    expect((await api.getTicket('DEMO-0053')).needs?.blocked).toBeNull()
  })
})

describe('ticket needs and the claim / start precheck', () => {
  it('the ticket document carries its skills, connections (with status) and exposed env names', async () => {
    const { api } = setup()
    const t = await api.getTicket('DEMO-0043')
    expect(t.needs).toEqual({
      skills: [
        { name: 'orch', needs: 'declared' },
        { name: 'dbt-seeds', needs: 'declared' },
        { name: 'github-prs', needs: 'declared' },
      ],
      connections: [
        { name: 'gh', status: 'ok' },
        { name: 'databricks-ci', status: 'ok' },
      ],
      env: ['DATABRICKS_HOST', 'DATABRICKS_TOKEN'],
      blocked: null,
    })
    expect((await api.getTicket('DEMO-0054')).needs?.blocked).toEqual({ connection: 'gcloud-billing', status: 'wrong_identity' })
    // Service down does not block (only auth or identity does).
    expect((await api.getTicket('DEMO-0049')).needs).toMatchObject({ connections: [{ name: 'tariff-api', status: 'service_down' }], blocked: null })
    expect((await api.getTicket('DEMO-0047')).needs?.skills).toContainEqual({ name: 'meter-notes', needs: 'unknown' })
    expect((await api.getTicket('DEMO-0041')).needs).toBeUndefined()
  })
  it('a claim on a ticket whose connection auth expired is refused and writes no ticket event', async () => {
    const { api, store } = setup()
    const before = store.eventsOf('DEMO-0053').length
    expect(await refusal(api.postAction('DEMO-0053', { action: 'claim' }))).toMatchObject({ status: 409, code: 'connection.blocked', message: 'DEMO-0053 is blocked: databricks-prod auth expired.' })
    expect(store.eventsOf('DEMO-0053').length).toBe(before)
  })
  it('a start refused for another reason (no grant) runs no check and records nothing', () => {
    const { store } = setup('p_mara')
    for (const g of store.grants(DEMO).filter((x) => x.person === 'p_mara' && !x.revoked)) store.revokeGrant(DEMO, g.id, { kind: 'person', id: 'p_mara' })
    const before = store.wsEventsOf(DEMO).length
    const res = store.startSession(DEMO, { ticket: 'DEMO-0054', mode: 'work', harness: 'claude-code', where: 'background', addon: 'start-agent' }, { kind: 'person', id: 'p_mara' })
    expect(res).toMatchObject({ code: 'grant.none' })
    expect(res).toMatchObject({ ok: false })
    expect((res as { code: string }).code).not.toBe('connection.blocked')
    expect(store.wsEventsOf(DEMO).length).toBe(before)
  })
  it('starting an agent is refused the same way (core, after the other refusals)', () => {
    const { store } = setup()
    const res = store.startSession(DEMO, { ticket: 'DEMO-0054', mode: 'work', harness: 'claude-code', where: 'background', addon: 'start-agent' }, { kind: 'person', id: 'p_sev' })
    expect(res).toMatchObject({ ok: false, status: 409, code: 'connection.blocked' })
  })
})

describe('the secrets file and the doctor', () => {
  it('shows the path, permissions and names only, with refused lines by number', async () => {
    const { api } = setup()
    const f = await api.getSecretsFile(DEMO)
    expect(f).toMatchObject({ exists: true, template: '<host state dir>/secrets/<workspace-uuid>.env', dir_mode: '0700', file_mode: '0600', owner_user: 'orch-agent', permissions_ok: true })
    expect(f.path).toMatch(new RegExp(`/secrets/${DEMO}\\.env$`))
    expect(f.names.map((n) => n.name)).toEqual(['DATABRICKS_HOST', 'DATABRICKS_TOKEN', 'TARIFF_API_TOKEN', 'OLD_WAREHOUSE_KEY'])
    expect(f.names.find((n) => n.name === 'DATABRICKS_TOKEN')).toMatchObject({ skills: ['dbt-seeds'], connections: ['databricks-ci'] })
    expect(f.problems).toEqual([
      { line: 8, reason: '"export" is not allowed: the file is parsed, not sourced' },
      { line: 9, reason: 'LEGACY_PIN is shorter than 8 characters: output filtering could not hide it' },
    ])
    expect(f.names.some((n) => n.name === 'SLACK_WEBHOOK' || n.name === 'LEGACY_PIN')).toBe(false)
    // The seeded agent on DEMO-0043 started before the 10:05 rotation and gets DATABRICKS_TOKEN.
    expect(f.stale_sessions).toEqual([{ session: 's_77c2', agent: 'Claude Code', ticket: 'DEMO-0043', started: '2026-10-09T08:05:30Z' }])
  })
  it('members and viewers do not see the secrets file', async () => {
    expect(await refusal(setup('p_tom').api.getSecretsFile(DEMO))).toMatchObject({ status: 403 })
  })
  it('the doctor runs every check and lists skills with unknown needs and ungranted references', async () => {
    const { api } = setup()
    const r = await api.runDoctor(DEMO)
    expect(r.checks.map((c) => `${c.name}:${c.status}`)).toEqual(['gh:ok', 'databricks-prod:auth_expired', 'gcloud-billing:wrong_identity', 'databricks-ci:ok', 'tariff-api:service_down', 'az-storage:unknown'])
    expect(r.unknown_skills).toEqual([
      { name: 'meter-notes', scope: 'workspace', path: 'skills/meter-notes/SKILL.md', needs: 'unknown' },
      { name: 'naming-conventions', scope: 'workspace', path: 'skills/naming-conventions/SKILL.md', needs: 'invalid' },
    ])
    expect(r.secrets_problems.map((p) => p.line)).toEqual([8, 9])
    expect(r.ungranted).toEqual([{ skill: 'tariff-feed', refs: ['TARIFF_WEBHOOK_SECRET'] }])
    expect(r.stale_sessions).toHaveLength(1)
  })
})

describe('the secrets file parser', () => {
  it('parses NAME=value literally: no sourcing, no expansion, no export', () => {
    const p = parseSecretsFile('# c\nA_TOKEN=$(rm -rf ~)\nB=$HOME/.x/yy\nexport C=1\nlower=1\nnot a line\nA_TOKEN=again\nD=\n')
    expect(p.entries).toEqual([
      { name: 'A_TOKEN', value: '$(rm -rf ~)', line: 2 },
      { name: 'B', value: '$HOME/.x/yy', line: 3 },
    ])
    expect(p.problems.map((x) => x.line)).toEqual([4, 5, 6, 7, 8])
    expect(JSON.stringify(p.problems)).not.toContain('again')
  })
})

describe('the busy day background script', () => {
  it('never takes a claim on a ticket blocked by a connection', async () => {
    const { claimable } = await import('./busy/live')
    const { store } = setup()
    const doc = store.ticket('DEMO-0053')!
    const open = { ...doc, status: 'open' as const, claim: null, gates: { ...doc.gates, plan: { ...doc.gates.plan, state: 'approved' as const } }, tasks: doc.tasks.length ? doc.tasks : [{ id: 'T1', text: 't' }] } as typeof doc
    expect(open.needs?.blocked).not.toBeNull()
    expect(claimable(open)).toBe(false)
    expect(claimable({ ...open, needs: { ...open.needs!, blocked: null } })).toBe(true)
  })
})
