import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TicketDefinition } from '@/api/types'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { getAddon } from './registry'
import { declaredOf, repoStatus, type ReposState } from './repos'

const ROOT = '~/work/acme'
const setup = (dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  const api = createApi(createMockTransport(store, { latency: false }))
  const state = store.addonState(ws, 'repos') as ReposState
  const run = (action: string, body: Record<string, unknown> = {}) => api.runAddonAction(ws, 'repos', action, body)
  const declared = () => store.workspaces.find((w) => w.id === ws)!.repos ?? {}
  const args = (name: string) => ({ name, remote: declared()[name].remote, target_folder: `${ROOT}/${declared()[name].path}`, clone_as: 'gh' })
  const as = (person: string) => store.setViewer(person)
  return { store, ws, api, state, run, args, declared, as, view: () => api.getAddonState(ws, 'repos') }
}
const addArgs = { name: 'new-repo', remote: 'https://git.example.test/acme/new-repo.git', path: 'new-repo', default_branch: 'main', target_folder: `${ROOT}/new-repo`, confirmed: true }
afterEach(() => vi.useRealTimers())

describe('Repos host: the declared list is settings.repos', () => {
  it('seeds settings.repos per workspace and keeps no second list in addon state', () => {
    for (const dataset of ['normal', 'busy'] as const) {
      const s = setup(dataset)
      expect(s.store.addons.find((a) => a.name === 'repos')).toMatchObject({ preview: true, capabilities: ['network', 'pty'] })
      expect(Object.keys(s.declared())).toHaveLength(dataset === 'normal' ? 7 : 17)
      expect(s.state).not.toHaveProperty('declared')
      for (const w of s.store.workspaces) expect(w.addons.repos.status).toBe('active')
    }
  })

  it.each(['normal', 'busy'] as const)('every links.repos name is in settings.repos (%s, format §5.11)', (dataset) => {
    const store = createMockStore({ persist: false, dataset })
    for (const w of store.workspaces) {
      for (const key of store.ticketKeys(w.id)) for (const name of store.ticket(key)!.links.repos) expect(Object.keys(w.repos ?? {}), `${key} → ${name}`).toContain(name)
    }
  })

  it('derives all five folder states; a repo without a declared remote is present by its folder', async () => {
    const s = setup()
    const r = declaredOf({ store: s.store, ws: s.ws }).find((x) => x.name === 'meter-ingest')!
    const o = s.state.observed[r.full]
    expect(repoStatus(r, o)).toBe('present')
    expect(repoStatus(r, undefined)).toBe('missing')
    expect(repoStatus(r, { ...o, git: false })).toBe('not a repo')
    expect(repoStatus(r, { ...o, remote: 'https://git.example.test/acme/other.git' })).toBe('remote differs')
    expect(repoStatus(r, { ...o, remote: 'git@git.example.test:acme/meter-ingest' })).toBe('present')
    expect(repoStatus(undefined, o)).toBe('untracked')
    expect(repoStatus({}, o)).toBe('present')
    s.state.disk[r.full].git = false
    await s.run('check')
    expect(s.state.observed[r.full].git).toBe(false)
  })

  it('declaring signs a settings.changed by the owner (person actor), folded into the workspace', async () => {
    const s = setup()
    await s.run('prepare_add', { formData: { remote: addArgs.remote } })
    expect(s.state.drafts.p_sev).toEqual({ name: 'new-repo', path: 'new-repo', remote: addArgs.remote, branch: 'main' })
    await expect(s.run('add', { ...addArgs, confirmed: undefined })).rejects.toMatchObject({ status: 409, code: 'confirm.required' })
    await s.run('add', addArgs)
    expect(s.declared()['new-repo']).toEqual({ path: 'new-repo', remote: addArgs.remote, default_branch: 'main' })
    const e = s.store.wsEventsOf(s.ws).filter((x) => x.type === 'settings.changed').at(-1)!
    expect(e.actor).toEqual({ kind: 'person', id: 'p_sev' })
    expect(e.set).toEqual({ repos: { 'new-repo': { path: 'new-repo', remote: addArgs.remote, default_branch: 'main' } } })
    expect(s.store.wsEventsOf(s.ws).some((x) => x.type === ('repos.added' as never))).toBe(false)
    expect(s.state.drafts).toEqual({})
  })

  it('add, adopt and remove are owner-only on the host (maintainer 403), clone stays maintainer', async () => {
    const s = setup()
    s.as('p_mara')
    await expect(s.run('prepare_add', { formData: { remote: addArgs.remote } })).rejects.toMatchObject({ status: 403 })
    await expect(s.run('add', addArgs)).rejects.toMatchObject({ status: 403 })
    await expect(s.run('adopt', { name: 'sandbox', path: 'sandbox', remote: 'x', target_folder: `${ROOT}/sandbox`, confirmed: true })).rejects.toMatchObject({ status: 403 })
    await expect(s.run('remove', { name: 'shared-lib', target_folder: `${ROOT}/shared-lib`, confirmed: true })).rejects.toMatchObject({ status: 403 })
    await expect(s.store.changeRepos(s.ws, { x: { path: 'x' } }, { kind: 'person', id: 'p_mara' })).toMatchObject({ ok: false, status: 403 })
    await s.run('clone', { ...s.args('billing-api'), confirmed: true })
    expect(s.state.jobs['billing-api']).toBeDefined()
  })

  it('core refuses an agent or addon actor, and two repos on one path', () => {
    const s = setup()
    expect(s.store.changeRepos(s.ws, { x: { path: 'x' } }, { kind: 'agent', id: 'claude' } as never)).toMatchObject({ ok: false, code: 'human_only' })
    expect(s.store.changeRepos(s.ws, { x: { path: './Web-Portal' } }, { kind: 'person', id: 'p_sev' })).toMatchObject({ ok: false, status: 409, code: 'settings.repos_same_path' })
    expect(s.store.changeRepos(s.ws, { 'bad name': { path: 'x' } }, { kind: 'person', id: 'p_sev' })).toMatchObject({ ok: false, status: 400 })
    expect(s.store.changeRepos(s.ws, { x: { path: 'x', remote: 'https://u:p@git.example.test/x.git' } }, { kind: 'person', id: 'p_sev' })).toMatchObject({ ok: false, status: 400 })
  })

  it('the core settings route takes the same signed change', async () => {
    const s = setup()
    await s.api.postSettings(s.ws, { op: 'repos', set: { tools: { path: 'tools' } } })
    expect(s.declared().tools).toEqual({ path: 'tools' })
    await expect(s.api.postSettings(s.ws, { op: 'repos', set: { other: { path: 'tools' } } })).rejects.toMatchObject({ status: 409 })
    s.as('p_mara')
    await expect(s.api.postSettings(s.ws, { op: 'repos', set: { tools: null } })).rejects.toMatchObject({ status: 403 })
  })
})

describe('Repos host: clone, fetch, check', () => {
  it('clones through queued and progress to present with the mock clock, signed with the git login', async () => {
    vi.useFakeTimers()
    const s = setup()
    await expect(s.run('clone', s.args('billing-api'))).rejects.toMatchObject({ status: 409, code: 'confirm.required' })
    await expect(s.run('clone', { ...s.args('billing-api'), clone_as: 'other', confirmed: true })).rejects.toMatchObject({ status: 409, code: 'repos.changed' })
    await s.run('clone', { ...s.args('billing-api'), confirmed: true })
    expect(JSON.stringify(await s.view())).toContain('queued')
    vi.advanceTimersByTime(3000)
    expect(JSON.stringify(await s.view())).toContain('cloning 50%')
    vi.advanceTimersByTime(4000)
    await s.view()
    expect(s.state.observed[`${ROOT}/billing-api`].remote).toBe(s.args('billing-api').remote)
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'repos.cloned')!.actor).toEqual({ kind: 'addon', id: 'repos' })
  })

  it('shows the clone identity from the connection; without a git login nothing clones', async () => {
    const s = setup()
    expect(JSON.stringify(await s.view())).toContain('gh · orch-agent-acme on github.com · OS user orch-agent')
    s.state.settings.connection = 'missing'
    await expect(s.run('clone', { ...s.args('billing-api'), clone_as: '', confirmed: true })).rejects.toMatchObject({ status: 409, code: 'repos.no_login' })
    expect((await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'repos')).toEqual([])
  })

  it('a declared repo without a remote cannot be cloned', async () => {
    const s = setup()
    await s.api.postSettings(s.ws, { op: 'repos', set: { bare: { path: 'bare' } } })
    await expect(s.run('clone', { name: 'bare', remote: '', target_folder: `${ROOT}/bare`, clone_as: 'gh', confirmed: true })).rejects.toMatchObject({ status: 409, code: 'repos.no_remote' })
  })

  it('clone all signs every target, fails one seeded clone and retries it', async () => {
    vi.useFakeTimers()
    const s = setup('busy')
    const targets = ['billing-api', 'private-api'].map((n) => `${s.args(n).remote} → ${s.args(n).target_folder}`).join('\n')
    await expect(s.run('clone_all', { targets: 'wrong', clone_as: 'gh', confirmed: true })).rejects.toMatchObject({ status: 409 })
    await s.run('clone_all', { targets, clone_as: 'gh', confirmed: true })
    vi.advanceTimersByTime(7000)
    await s.view()
    expect(s.state.jobs['private-api'].error).toBe('Repository not found or no access')
    expect(s.state.observed[`${ROOT}/billing-api`]).toBeDefined()
    await s.run('clone', { ...s.args('private-api'), confirmed: true })
    vi.advanceTimersByTime(7000)
    await s.view()
    expect(s.state.observed[`${ROOT}/private-api`]).toBeDefined()
  })

  it('refuses stale targets and never overwrites an existing folder', async () => {
    const s = setup()
    await expect(s.run('clone', { ...s.args('billing-api'), target_folder: '/tmp/elsewhere', confirmed: true })).rejects.toMatchObject({ status: 409 })
    await expect(s.run('clone', { ...s.args('meter-ingest'), confirmed: true })).rejects.toMatchObject({ status: 409 })
    expect(s.state.jobs).toEqual({})
  })

  it('fetch refreshes tracking data without pulling or discarding dirty work', async () => {
    vi.useFakeTimers()
    const s = setup()
    const at = `${ROOT}/web-portal`
    const old = s.state.observed[at].fetched
    vi.advanceTimersByTime(5000)
    await s.run('fetch', { name: 'web-portal' })
    expect(s.state.observed[at]).toMatchObject({ dirty: 2, behind: 1 })
    expect(s.state.observed[at].fetched).not.toBe(old)
    await s.run('fetch_all')
    expect(s.state.observed[`${ROOT}/meter-ingest`].behind).toBe(4)
    expect(s.state.observed[`${ROOT}/docs-site`].behind).toBe(0) // remote differs: not fetched
  })

  it('settings are host validated; auto-check with fetch follows the mock clock and is by orch', async () => {
    vi.useFakeTimers()
    const s = setup()
    await expect(s.run('save_settings', { formData: { interval: 'fast', fetch: true } })).rejects.toMatchObject({ status: 400 })
    await expect(s.run('save_settings', { formData: { interval: '1 h', fetch: true, connection: 'tariff-api' } })).rejects.toMatchObject({ status: 400 })
    await expect(s.run('save_settings', { formData: { interval: '1 h', fetch: true, connection: 'databricks-prod' } })).rejects.toMatchObject({ status: 400 })
    await s.run('save_settings', { formData: { interval: '15 min', fetch: true, connection: 'gh' } })
    const old = s.state.checked
    vi.advanceTimersByTime(900001)
    await s.view()
    expect(s.state.checked).not.toBe(old)
    expect(s.state.log[0]).toMatchObject({ type: 'repos.checked', by: 'orch' })
    expect(s.state.observed[`${ROOT}/meter-ingest`].behind).toBe(4)
  })
})

describe('Repos host: declaration validation', () => {
  it.each([
    'https://user:token@git.example.test/a.git',
    'https://user@git.example.test/a.git',
    'https://:@git.example.test/a.git',
    'https://us%65r@git.example.test/a.git',
    'ssh://user:pass@git.example.test/a.git',
    'git@-oProxyCommand=x:y',
    'ssh://-oProxyCommand=x/y',
    'git@git.example.test:-u/x.git',
    'file:///tmp/repo',
    'https://gіt.example.test/a.git',
    'https://git.example.test/a.git?secret=x',
  ])('refuses unsafe remote at the host before any draft: %s', async (remote) => {
    const s = setup()
    await expect(s.run('add', { ...addArgs, remote })).rejects.toMatchObject({ status: 400 })
    await expect(s.run('prepare_add', { formData: { remote } })).rejects.toMatchObject({ status: 400 })
    expect(s.state.drafts).toEqual({})
    expect(s.state.log).toEqual([])
    expect(JSON.stringify(s.store.wsEventsOf(s.ws))).not.toContain(remote)
  })

  it.each(['..', 'a..b', 'a/b', '/abs', '~/x', '-bad', '.git', 'bіlling', 'a'.repeat(101)])('refuses bad folder %s', async (path) => {
    await expect(setup().run('add', { ...addArgs, path, target_folder: `${ROOT}/${path}` })).rejects.toMatchObject({ status: 400 })
  })

  it('refuses duplicate names, folders (case-insensitive) and remotes across spellings', async () => {
    const s = setup()
    await expect(s.run('add', { ...addArgs, name: 'Web-Portal', path: 'x', target_folder: `${ROOT}/x` })).rejects.toMatchObject({ code: 'repos.duplicate_name' })
    await expect(s.run('add', { ...addArgs, path: 'Web-Portal', target_folder: `${ROOT}/Web-Portal` })).rejects.toMatchObject({ code: 'repos.duplicate_folder' })
    await expect(s.run('add', { ...addArgs, path: 'sandbox', target_folder: `${ROOT}/sandbox` })).rejects.toMatchObject({ code: 'repos.duplicate_folder' })
    await expect(s.run('add', { ...addArgs, remote: 'git@git.example.test:acme/web-portal' })).rejects.toMatchObject({ code: 'repos.duplicate_remote' })
    await expect(s.run('add', { ...addArgs, target_folder: '~/elsewhere' })).rejects.toMatchObject({ code: 'repos.changed' })
  })

  it('declares an untracked folder only with its exact observed remote', async () => {
    const s = setup()
    const args = { name: 'sandbox', path: 'sandbox', remote: s.state.observed[`${ROOT}/sandbox`].remote, target_folder: `${ROOT}/sandbox`, confirmed: true }
    await expect(s.run('adopt', { ...args, remote: 'https://git.example.test/other.git' })).rejects.toMatchObject({ status: 409 })
    await s.run('adopt', args)
    expect(s.declared().sandbox).toMatchObject({ path: 'sandbox', remote: args.remote })
  })
})

describe('Repos host: removal', () => {
  it('refuses linked open tickets, requires the explicit override, and leaves the disk', async () => {
    const s = setup()
    const args = { name: 'web-portal', target_folder: `${ROOT}/web-portal`, confirmed: true }
    await expect(s.run('remove', args)).rejects.toMatchObject({ status: 409, code: 'repos.linked' })
    await expect(s.run('remove_anyway', { ...args, choice: 'keep' })).rejects.toMatchObject({ status: 400 })
    await s.run('remove_anyway', { ...args, choice: 'remove' })
    expect(s.declared()['web-portal']).toBeUndefined()
    expect(s.state.disk[`${ROOT}/web-portal`].dirty).toBe(2)
    expect(s.store.wsEventsOf(s.ws).at(-1)).toMatchObject({ type: 'settings.changed', set: { repos: { 'web-portal': null } }, actor: { kind: 'person', id: 'p_sev' } })
  })

  it('a restricted ticket the owner cannot see is neither counted nor revealed', async () => {
    const s = setup()
    const def = (s.store as unknown as { defs: Map<string, TicketDefinition> }).defs.get('DEMO-0044')!
    def.links.repos = ['shared-lib']
    def.visibility = { restricted: ['p_mara'] }
    expect(s.store.isVisible(def.key)).toBe(false)
    const page = (await s.view()) as unknown as { page: { tabs: { node: { children: { rowDetail?: { nodes: Record<string, unknown> } }[] } }[] } }
    const shared = JSON.stringify(page.page.tabs[0].node.children.find((n) => n.rowDetail)!.rowDetail!.nodes['shared-lib'])
    expect(shared).not.toContain('Remove anyway')
    expect(shared).toContain('"Linked open tickets","value":0')
    await s.run('remove', { name: 'shared-lib', target_folder: `${ROOT}/shared-lib`, confirmed: true })
    expect(s.declared()['shared-lib']).toBeUndefined()
  })

  it('the ticket filter by repo is exact and respects visibility', async () => {
    const s = setup()
    const keys = (await s.api.listTickets(s.ws, { repo: 'web-portal' })).map((t) => t.key)
    expect(keys).toEqual(['DEMO-0046'])
    s.as('p_tom')
    expect((await s.api.listTickets(s.ws, { repo: 'acme-energy-dbt' })).every((t) => s.store.isVisible(t.key, 'p_tom'))).toBe(true)
  })
})

describe('Repos host: roles, terminal, Today', () => {
  it('enforces every manifest role with host 403 refusals', async () => {
    const s = setup()
    const pkg = s.store.addons.find((a) => a.name === 'repos')!
    const levels = ['viewer', 'member', 'maintainer', 'owner'] as const
    const me = s.store.workspaces.find((w) => w.id === s.ws)!.members.find((m) => m.person === 'p_sev')!
    for (const [id, meta] of Object.entries(pkg.actions!)) {
      for (const role of levels.slice(0, levels.indexOf(meta.minRole ?? 'member'))) {
        me.role = role
        await expect(s.run(id, { confirmed: true })).rejects.toMatchObject({ status: 403 })
      }
    }
    expect(pkg.actions).toMatchObject({ add: { minRole: 'owner' }, adopt: { minRole: 'owner' }, remove: { minRole: 'owner' }, remove_anyway: { minRole: 'owner' }, clone: { minRole: 'maintainer' } })
  })

  it('the page offers only the buttons the viewer may use', async () => {
    const s = setup()
    s.as('p_tom')
    const tom = JSON.stringify(await s.view())
    for (const a of ['"clone"', '"fetch"', '"add"', '"remove"', '"prepare_add"', '"check"']) expect(tom).not.toContain(`"action":${a}`)
    s.as('p_mara')
    const mara = JSON.stringify(await s.view())
    expect(mara).toContain('"action":"clone"')
    expect(mara).not.toContain('"action":"prepare_add"')
    expect(mara).not.toContain('"action":"remove"')
  })

  it('opens a member-owned dock shell with cd typed, never executed; pty required', async () => {
    const s = setup()
    const result = await s.run('open_terminal', { name: 'web-portal' })
    expect(result.terminal).toBeTruthy()
    const terminals = JSON.stringify(await s.api.getAddonState(s.ws, 'terminals'))
    expect(terminals).toContain(`cd -- \\"$HOME\\"/'work/acme/web-portal'`)
    await expect(s.run('open_terminal', { name: '../../etc' })).rejects.toMatchObject({ status: 404 })
    s.store.workspaces.find((w) => w.id === s.ws)!.addons.repos.granted = null
    await expect(s.run('open_terminal', { name: 'web-portal' })).rejects.toMatchObject({ status: 409 })
  })

  it('Today decisions exist only for missing repos and their terms are checked', async () => {
    const s = setup()
    const d = (await s.api.getAddonDecisions(s.ws)).find((x) => x.addon === 'repos')!
    expect(d.terms).toEqual(s.args('billing-api'))
    await s.run('clone_attention', { id: d.id, terms: d.terms, option: 'clone', confirmed: true })
    expect((await s.api.getAddonDecisions(s.ws)).filter((x) => x.addon === 'repos')).toEqual([])
    expect(getAddon('repos')).toBeDefined()
  })
})
