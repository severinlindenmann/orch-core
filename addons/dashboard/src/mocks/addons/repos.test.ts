import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TicketDefinition } from '@/api/types'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { getAddon } from './registry'
import { repoStatus, type ReposState } from './repos'

const setup = (dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  const ws = store.workspaces.find(w => w.prefix === 'DEMO')!.id
  const api = createApi(createMockTransport(store, { latency: false }))
  const state = store.addonState(ws, 'repos') as ReposState
  const run = (action: string, body: Record<string, unknown> = {}) => api.runAddonAction(ws, 'repos', action, body)
  const args = (name: string) => {
    const r = state.declared.find(r => r.name === name)!
    return { name, remote: r.remote, target_folder: `~/work/acme/${r.folder}` }
  }
  return { store, ws, api, state, run, args, view: () => api.getAddonState(ws, 'repos') }
}
const addArgs = { name: 'new-repo', remote: 'https://git.example.test/acme/new-repo.git', folder: 'new-repo', branch: 'main', primary: false, target_folder: '~/work/acme/new-repo', confirmed: true }
afterEach(() => vi.useRealTimers())

describe('Repos host', () => {
  it('installs a preview in each workspace and seeds normal and busy states', () => {
    for (const dataset of ['normal', 'busy'] as const) {
      const s = setup(dataset)
      expect(s.store.addons.find(a => a.name === 'repos')).toMatchObject({ preview: true, capabilities: ['network', 'pty'] })
      expect(s.state.declared).toHaveLength(dataset === 'normal' ? 6 : 14)
      for (const w of s.store.workspaces) {
        expect(w.addons.repos.status).toBe('active')
        if (w.prefix !== 'DEMO') expect((s.store.addonState(w.id, 'repos') as ReposState).declared).toHaveLength(2)
      }
    }
  })
  it('derives all five folder states and checks a fresh disk snapshot', async () => {
    const s = setup()
    const r = s.state.declared[1], o = s.state.observed[r.folder]
    expect(repoStatus(r, o)).toBe('present')
    expect(repoStatus(r, undefined)).toBe('missing')
    expect(repoStatus(r, { ...o, git: false })).toBe('not a repo')
    expect(repoStatus(r, { ...o, remote: 'different' })).toBe('remote differs')
    expect(repoStatus(undefined, o)).toBe('untracked')
    s.state.disk[r.folder].git = false
    expect(s.state.observed[r.folder].git).toBe(true)
    await s.run('check')
    expect(s.state.observed[r.folder].git).toBe(false)
  })
  it('clones through queued and progress to present with the mock clock', async () => {
    vi.useFakeTimers()
    const s = setup()
    await expect(s.run('clone', s.args('billing-api'))).rejects.toMatchObject({ status: 409, code: 'confirm.required' })
    await s.run('clone', { ...s.args('billing-api'), confirmed: true })
    expect(JSON.stringify(await s.view())).toContain('queued')
    vi.advanceTimersByTime(3000)
    expect(JSON.stringify(await s.view())).toContain('cloning 50%')
    vi.advanceTimersByTime(4000)
    await s.view()
    expect(s.state.observed['billing-api'].remote).toBe(s.args('billing-api').remote)
    expect(s.state.log.some(l => l.type === 'repos.cloned')).toBe(true)
  })
  it('clone all signs every target, fails one seeded clone and retries it', async () => {
    vi.useFakeTimers()
    const s = setup('busy')
    const targets = ['billing-api', 'private-api'].map(n => `${s.args(n).remote} → ${s.args(n).target_folder}`).join('\n')
    await expect(s.run('clone_all', { targets: 'wrong', confirmed: true })).rejects.toMatchObject({ status: 409 })
    await s.run('clone_all', { targets, confirmed: true })
    vi.advanceTimersByTime(7000); await s.view()
    expect(s.state.jobs['private-api'].error).toBe('Repository not found or no access')
    expect(s.state.observed['billing-api']).toBeDefined()
    await s.run('clone', { ...s.args('private-api'), confirmed: true })
    vi.advanceTimersByTime(7000); await s.view()
    expect(s.state.observed['private-api']).toBeDefined()
  })
  it('refuses stale targets and never overwrites an existing folder', async () => {
    const s = setup()
    await expect(s.run('clone', { ...s.args('billing-api'), target_folder: '/tmp/elsewhere', confirmed: true })).rejects.toMatchObject({ status: 409 })
    await expect(s.run('clone', { ...s.args('meter-ingest'), confirmed: true })).rejects.toMatchObject({ status: 409 })
    expect(s.state.jobs).toEqual({})
  })
  it('fetch refreshes tracking data without pulling or discarding dirty work', async () => {
    vi.useFakeTimers()
    const s = setup(), old = s.state.observed['web-portal'].fetched
    vi.advanceTimersByTime(5000)
    await s.run('fetch', { name: 'web-portal' })
    expect(s.state.observed['web-portal']).toMatchObject({ dirty: 2, behind: 1 })
    expect(s.state.observed['web-portal'].fetched).not.toBe(old)
    await s.run('fetch_all')
    expect(s.state.observed['meter-ingest'].behind).toBe(4)
    expect(s.state.observed['docs-site'].behind).toBe(0)
  })
  it.each(['https://user:token@git.example.test/a.git', 'https://user@git.example.test/a.git', 'ssh://git@git.example.test/a.git', 'https://@git.example.test/a.git', 'file:///tmp/repo', 'https:git.example.test/repo', 'https://git.example.test\\repo', 'https://git.example.test/a.git?secret=x', 'https://git.example.test/a\n.git'])('refuses unsafe remote at the host: %s', async remote => {
    const s = setup()
    await expect(s.run('add', { ...addArgs, remote })).rejects.toMatchObject({ status: 400 })
    await expect(s.run('prepare_add', { formData: { remote } })).rejects.toMatchObject({ status: 400 })
    expect(s.state.drafts).toEqual({})
    expect(s.state.log).toEqual([])
  })
  it.each(['..', 'a..b', 'a/b', '-bad', 'UPPER', 'a'.repeat(65)])('refuses bad folder %s', async folder => {
    await expect(setup().run('add', { ...addArgs, folder })).rejects.toMatchObject({ status: 400 })
  })
  it('defaults the folder, signs add, enforces duplicates and one primary', async () => {
    const s = setup()
    await s.run('prepare_add', { formData: { remote: addArgs.remote } })
    expect(s.state.drafts.p_sev.folder).toBe('new-repo')
    await s.run('add', { ...addArgs, primary: true })
    expect(s.state.declared.filter(r => r.primary).map(r => r.name)).toEqual(['new-repo'])
    await expect(s.run('add', addArgs)).rejects.toMatchObject({ status: 409, code: 'repos.duplicate_folder' })
    await expect(s.run('add', { ...addArgs, folder: 'different' })).rejects.toMatchObject({ status: 409, code: 'repos.duplicate_remote' })
  })
  it('accepts credential-free SSH spellings and refuses duplicate transport identities', async () => {
    const s = setup()
    await s.run('prepare_add', { formData: { remote: 'git@git.example.test:repo.git' } })
    expect(s.state.drafts.p_sev.folder).toBe('repo')
    await s.run('add', { ...addArgs, remote: 'ssh://git.example.test:22/acme/new-repo.git' })
    await expect(s.run('add', { ...addArgs, name: 'alias', folder: 'alias', target_folder: '~/work/acme/alias' })).rejects.toMatchObject({ status: 409, code: 'repos.duplicate_remote' })
  })
  it('adopts only the exact observed untracked remote', async () => {
    const s = setup()
    const args = { name: 'sandbox', folder: 'sandbox', remote: s.state.observed.sandbox.remote, target_folder: '~/work/acme/sandbox', confirmed: true }
    await expect(s.run('adopt', { ...args, remote: 'https://git.example.test/other.git' })).rejects.toMatchObject({ status: 409 })
    await s.run('adopt', args)
    expect(s.state.declared.find(r => r.name === 'sandbox')).toBeDefined()
  })
  it('removal refuses linked open tickets, requires explicit override, and preserves disk', async () => {
    const s = setup()
    const args = { name: 'web-portal', target_folder: '~/work/acme/web-portal', confirmed: true }
    await expect(s.run('remove', args)).rejects.toMatchObject({ status: 409, code: 'repos.linked' })
    await expect(s.run('remove', { ...args, choice: 'remove' })).rejects.toMatchObject({ status: 409 })
    await expect(s.run('remove_anyway', { ...args, choice: 'keep' })).rejects.toMatchObject({ status: 400 })
    await s.run('remove_anyway', { ...args, choice: 'remove' })
    expect(s.state.declared.some(r => r.name === 'web-portal')).toBe(false)
    expect(s.state.disk['web-portal'].dirty).toBe(2)
    const tickets = await s.api.listTickets(s.ws, { repo: 'web-portal' })
    expect(tickets.map(t => t.key)).toContain('DEMO-0046')
    expect(tickets.every(t => t.status !== 'done')).toBe(true)
  })
  it('protects a restricted open ticket even when the owner cannot read it', async () => {
    const s = setup()
    const def = (s.store as unknown as { defs: Map<string, TicketDefinition> }).defs.get('DEMO-0044')!
    def.links.repos = ['shared-lib']
    def.visibility = { restricted: ['p_mara'] }
    expect(s.store.isVisible(def.key)).toBe(false)
    await expect(s.run('remove', { name: 'shared-lib', target_folder: '~/work/acme/shared-lib', confirmed: true })).rejects.toMatchObject({ status: 409, code: 'repos.linked' })
  })
  it('plain removal leaves an untracked folder and records the addon actor', async () => {
    const s = setup()
    const append = vi.spyOn(s.store, 'appendWs')
    await s.run('remove', { name: 'shared-lib', target_folder: '~/work/acme/shared-lib', confirmed: true })
    expect(s.state.disk['shared-lib']).toBeDefined()
    expect(append).toHaveBeenCalledWith(s.ws, expect.objectContaining({ type: 'repos.removed', actor: { kind: 'addon', id: 'repos' } }))
  })
  it('enforces every manifest role with host 403 refusals', async () => {
    const s = setup()
    const pkg = s.store.addons.find(a => a.name === 'repos')!
    const levels = ['viewer', 'member', 'maintainer', 'owner'] as const
    const me = s.store.workspaces.find(w => w.id === s.ws)!.members.find(m => m.person === 'p_sev')!
    for (const [id, meta] of Object.entries(pkg.actions!)) {
      for (const role of levels.slice(0, levels.indexOf(meta.minRole))) {
        me.role = role
        await expect(s.run(id, { confirmed: true })).rejects.toMatchObject({ status: 403 })
      }
    }
  })
  it('settings are host validated; auto-check with fetch follows the mock clock', async () => {
    vi.useFakeTimers()
    const s = setup()
    await expect(s.run('save_settings', { formData: { interval: 'fast', fetch: true } })).rejects.toMatchObject({ status: 400 })
    await s.run('save_settings', { formData: { interval: '15 min', fetch: true } })
    const old = s.state.checked
    vi.advanceTimersByTime(900001); await s.view()
    expect(s.state.checked).not.toBe(old)
    expect(s.state.observed['meter-ingest'].behind).toBe(4)
  })
  it('opens a member-owned dock shell with cd typed, never executed; pty required', async () => {
    const s = setup()
    const result = await s.run('open_terminal', { name: 'web-portal' })
    expect(result.terminal).toBeTruthy()
    const terminals = await s.api.getAddonState(s.ws, 'terminals')
    expect(JSON.stringify(terminals)).toContain('cd --')
    expect(JSON.stringify(terminals)).toContain('web-portal')
    s.store.workspaces.find(w => w.id === s.ws)!.addons.repos.granted = null
    await expect(s.run('open_terminal', { name: 'web-portal' })).rejects.toMatchObject({ status: 409 })
  })
  it('Today decisions exist only for missing repos and their terms are checked', async () => {
    const s = setup()
    const d = (await s.api.getAddonDecisions(s.ws)).find(d => d.addon === 'repos')!
    expect(d.terms).toEqual(s.args('billing-api'))
    await s.run('clone_attention', { id: d.id, terms: d.terms, option: 'clone', confirmed: true })
    expect((await s.api.getAddonDecisions(s.ws)).filter(d => d.addon === 'repos')).toEqual([])
    expect(getAddon('repos')).toBeDefined()
  })
})
