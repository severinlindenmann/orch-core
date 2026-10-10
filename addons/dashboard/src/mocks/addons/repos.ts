import { grantCovers } from '@/api/addons'
import type { AddonDecision } from '@/api/types'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon, type AddonCtx } from './registry'
import { openRepoShell } from './terminals'

export interface Repo {
  name: string
  folder: string
  remote: string
  branch: string
  primary: boolean
}
export interface Observed {
  git: boolean
  remote: string
  branch: string
  ahead: number
  behind: number
  dirty: number
  fetched: string | null
  size: string
  worktrees: number
}
interface Job { at: string; by: string; attempt: number; error?: string; done?: boolean }
export interface ReposState extends Record<string, unknown> {
  declared: Repo[]
  disk: Record<string, Observed>
  observed: Record<string, Observed>
  jobs: Record<string, Job>
  log: { type: string; at: string; by: string; text: string }[]
  checked: string
  settings: { interval: string; fetch: boolean }
  drafts: Record<string, Repo>
}
type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
const stateOf = (s: Record<string, unknown>) => s as ReposState
const rootOf = (c: Pick<Ctx, 'store' | 'ws'>) => c.store.workspaces.find(w => w.id === c.ws)?.root_folder ?? '~/work/workspace'
const target = (c: Ctx, r: Repo) => `${rootOf(c)}/${r.folder}`
const remoteOf = (name: string) => `https://git.example.test/acme/${name}.git`
const observation = (remote: string): Observed => ({ git: true, remote, branch: 'main', ahead: 0, behind: 0, dirty: 0, fetched: '2026-10-09T10:00:00Z', size: '128 MB', worktrees: 1 })
export const repoStatus = (repo: Repo | undefined, observed: Observed | undefined) => !repo ? 'untracked' : !observed ? 'missing' : !observed.git ? 'not a repo' : repo.remote !== observed.remote ? 'remote differs' : 'present'
export function seedRepos(ws: string, store: MockStore, busy = false): ReposState {
  const demo = store.workspaces.find(w => w.id === ws)?.prefix === 'DEMO'
  const names = demo ? ['billing-api', 'meter-ingest', 'web-portal', 'docs-site', 'infra', 'shared-lib', ...(busy ? ['analytics', 'notifications', 'identity', 'mobile', 'search', 'audit', 'reports', 'private-api'] : [])] : ['service', 'tooling']
  const declared = names.map((name, i) => ({ name, folder: name, remote: name === 'infra' ? 'git@git.example.test:acme/infra.git' : remoteOf(name), branch: 'main', primary: i === 0 }))
  const disk: Record<string, Observed> = {}
  declared.forEach((r, i) => {
    if (i === 0 || (busy && r.name === 'private-api')) return
    disk[r.folder] = { ...observation(r.remote), behind: i === 1 ? 3 : busy && i > 5 ? i % 4 : 0, dirty: i === 2 ? 2 : 0, remote: i === 3 ? remoteOf('old-docs') : r.remote }
  })
  disk['sandbox'] = observation(remoteOf('sandbox'))
  return { declared, disk, observed: structuredClone(disk), jobs: {}, log: [], checked: store.now(), settings: { interval: 'off', fetch: false }, drafts: {} }
}
const ok = (message: string) => ({ ok: true as const, message, changed: true })
function record(s: ReposState, c: Ctx, verb: 'added' | 'removed' | 'checked' | 'clone_queued' | 'cloned' | 'clone_failed' | 'fetched', text: string, by = c.viewer) {
  const type = `repos.${verb}` as const
  s.log.unshift({ type, at: c.store.now(), by, text })
  s.log = s.log.slice(0, 200)
  c.store.appendWs(c.ws, { type, actor: { kind: 'addon', id: 'repos' }, person: by, text })
}
// Removal protects every ticket, including restricted tickets the owner cannot read. Only the count leaves the host.
const protectedCount = (c: Ctx, name: string) => c.store.ticketKeys(c.ws).filter(key => {
  const t = c.store.ticket(key)
  return t && t.status !== 'done' && t.links.repos.includes(name)
}).length
const cloneArgs = (c: Ctx, r: Repo) => ({ name: r.name, remote: r.remote, target_folder: target(c, r) })
const missing = (s: ReposState) => s.declared.filter(r => !s.observed[r.folder] && !running(s.jobs[r.name]))
const running = (job?: Job) => !!job && !job.done && !job.error
const plan = (s: ReposState, c: Ctx) => missing(s).map(r => `${r.remote} → ${target(c, r)}`).join('\n')
function tick(s: ReposState, c: Ctx) {
  for (const r of s.declared) {
    const j = s.jobs[r.name]
    if (!running(j) || Date.parse(c.store.now()) - Date.parse(j.at) < 6000) continue
    if (r.name === 'private-api' && j.attempt === 1) {
      j.error = 'Repository not found or no access'
      record(s, c, 'clone_failed', `${r.name}: ${j.error}`, j.by)
    } else {
      // Never overwrite a folder that appeared after the clone was queued.
      if (s.disk[r.folder]) { j.error = 'Target folder now exists. Check before retrying.'; continue }
      s.disk[r.folder] = { ...observation(r.remote), branch: r.branch, fetched: c.store.now() }
      s.observed[r.folder] = structuredClone(s.disk[r.folder])
      j.done = true
      record(s, c, 'cloned', `Cloned ${r.name} into ${target(c, r)}.`, j.by)
    }
  }
  const ms: Record<string, number> = { '15 min': 900000, '1 h': 3600000, daily: 86400000 }
  if (ms[s.settings.interval] && Date.parse(c.store.now()) - Date.parse(s.checked) >= ms[s.settings.interval]) check(s, c)
}
function check(s: ReposState, c: Ctx) {
  if (s.settings.fetch) fetchRepos(s, c, s.declared)
  s.observed = structuredClone(s.disk)
  s.checked = c.store.now()
  record(s, c, 'checked', 'Checked all repo folders.')
}
function fetchRepos(s: ReposState, c: Ctx, repos: Repo[]) {
  for (const r of repos) {
    const o = s.disk[r.folder]
    if (!o?.git || o.remote !== r.remote) continue
    // Fetch refreshes tracking information; it never pulls or clears uncommitted work.
    o.fetched = c.store.now()
    o.behind += 1
    s.observed[r.folder] = structuredClone(o)
    record(s, c, 'fetched', `Fetched ${r.name}.`)
  }
}
function queue(s: ReposState, c: Ctx, r: Repo) {
  s.jobs[r.name] = { at: c.store.now(), by: c.viewer, attempt: (s.jobs[r.name]?.attempt ?? 0) + 1 }
  record(s, c, 'clone_queued', `Queued ${r.name}.`)
}
/** Host validation: reject credentials before storing any draft, event or signed value. */
export function remoteProblem(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 1800 || /[\s\p{Cc}\p{Cf}]/u.test(value)) return 'Use a credential-free HTTPS or SSH remote URL.'
  if (/^git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+$/.test(value)) return null
  if (!/^(https|ssh):\/\//.test(value) || value.includes('\\')) return 'Use an HTTPS remote, git@host:path, or ssh://host/path.'
  try {
    const u = new URL(value)
    if (u.username || u.password || /:\/\/[^/]*@/.test(value)) return 'Embedded credentials or userinfo are not allowed. Use a credential-free remote URL.'
    if (!['https:', 'ssh:'].includes(u.protocol) || !u.hostname || u.pathname === '/' || u.search || u.hash) return 'Use an HTTPS or SSH remote with a repository path and no query or fragment.'
    return null
  } catch { return 'Use an HTTPS remote, git@host:path, or ssh://host/path.' }
}
/** Canonical transport identity for duplicate detection; credentials have already been rejected. */
function remoteIdentity(remote: string): string {
  const u = new URL(remote.startsWith('git@') ? `ssh://${remote.slice(4).replace(':', '/')}` : remote)
  const port = u.port && !((u.protocol === 'ssh:' && u.port === '22') || (u.protocol === 'https:' && u.port === '443')) ? `:${u.port}` : ''
  return `https://${u.hostname.toLowerCase()}${port}${u.pathname.replace(/\/$/, '').replace(/\.git$/, '')}`
}
function validateRepo(s: ReposState, data: Record<string, unknown>, adopting = false): Repo | ReturnType<typeof invalid> {
  const problem = remoteProblem(data.remote)
  if (problem) return invalid(problem)
  const remote = data.remote as string
  const folder = data.folder === '' || data.folder === undefined ? remote.split(/[/:]/).pop()!.replace(/\.git$/, '') : data.folder
  if (typeof folder !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/.test(folder) || folder.includes('..')) return invalid('Folder must be 1–64 lowercase letters, digits, dots, underscores or dashes; no .. or slashes.')
  if (s.declared.some(r => r.folder === folder) || (!adopting && s.disk[folder])) return conflict('repos.duplicate_folder', 'That folder is already used. Adopt an untracked repo instead.')
  if (s.declared.some(r => remoteIdentity(r.remote) === remoteIdentity(remote))) return conflict('repos.duplicate_remote', 'That remote is already declared.')
  const branch = data.branch ?? 'main'
  if (typeof branch !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/.test(branch) || branch.includes('..') || branch.endsWith('/')) return invalid('Write a valid default branch.')
  if (data.primary !== undefined && typeof data.primary !== 'boolean') return invalid('Primary must be true or false.')
  return { name: folder, folder, remote, branch, primary: data.primary === true }
}
function add(s: ReposState, c: Ctx, r: Repo) {
  if (r.primary) s.declared.forEach(x => { x.primary = false })
  s.declared.push(r)
  record(s, c, 'added', `Declared ${r.name}.`)
}
const button = (label: string, action: string, args?: Record<string, string | boolean>) => ({ type: 'button', label, action, ...(args ? { args } : {}) })
const stack = (...children: unknown[]) => ({ type: 'stack', children })
const kv = (pairs: Record<string, string | number>) => ({ type: 'kv', pairs: Object.entries(pairs).map(([label, value]) => ({ label, value })) })
const repoLink = (name: string) => ({ type: 'link', label: `Open ${name} in Repos`, href: `/addon/repos/repos?repo=${encodeURIComponent(name)}` })
function view(s: ReposState, c: Ctx) {
  tick(s, c)
  const all = [...s.declared, ...Object.keys(s.observed).filter(folder => s.observed[folder].git && !s.declared.some(r => r.folder === folder)).map(folder => ({ name: folder, folder, remote: s.observed[folder].remote, branch: 'main', primary: false }))]
  const tickets = c.store.listTickets(c.ws)
  const protectedNames = new Set(c.store.roleIn(c.ws, c.viewer) === 'owner' ? c.store.ticketKeys(c.ws).flatMap(key => {
    const t = c.store.ticket(key)
    return t && t.status !== 'done' ? t.links.repos : []
  }) : [])
  const details: Record<string, unknown> = {}
  const issues: { title: string; id: string }[] = []
  const rows = all.map(r => {
    const declared = s.declared.includes(r)
    const o = s.observed[r.folder]
    const status = repoStatus(declared ? r : undefined, o)
    const job = s.jobs[r.name]
    const elapsed = job ? Date.parse(c.store.now()) - Date.parse(job.at) : 0
    const state = running(job) ? elapsed < 1000 ? 'queued' : `cloning ${Math.min(95, Math.floor(elapsed / 60))}%` : status
    if (status !== 'present') issues.push({ id: r.name, title: `${r.name}: ${status === 'missing' ? 'missing — not cloned yet' : status === 'remote differs' ? `remote differs: expected ${r.remote}, found ${o.remote}` : status}` })
    if (o?.behind) issues.push({ id: `${r.name}-behind`, title: `${r.name}: ${o.behind} behind origin/${r.branch}` })
    if (o?.dirty) issues.push({ id: `${r.name}-dirty`, title: `${r.name}: ${o.dirty} uncommitted changes` })
    const count = tickets.filter(t => t.status !== 'done' && t.links.repos.includes(r.name)).length
    details[r.name] = stack(
      /^https:\/\//.test(r.remote) ? { type: 'link', label: 'Remote URL', href: r.remote, copy: true } : kv({ 'Remote URL': r.remote }),
      kv({ 'Default branch': r.branch, 'Current branch': o?.branch ?? '—', 'Ahead / behind': o ? `${o.ahead} / ${o.behind}` : '—', 'Uncommitted changes': o?.dirty ?? '—', 'Last fetch': o?.fetched ?? 'Never', 'Size on disk': o?.size ?? '—', Worktrees: o?.worktrees ?? 0, 'Linked open tickets': count, ...(status === 'remote differs' ? { 'Observed remote': o.remote } : {}) }),
      { type: 'link', label: `${count} linked open tickets`, href: `/tickets?repo=${encodeURIComponent(r.name)}` },
      ...(job?.error ? [{ type: 'alert', tone: 'error', title: job.error }] : []),
      ...(declared && status === 'missing' && !running(job) ? [button(job?.error ? 'Retry' : 'Clone', 'clone', cloneArgs(c, r))] : []),
      ...(!declared ? [button('Adopt untracked', 'adopt', { ...cloneArgs(c, r), folder: r.folder })] : []),
      ...(o?.git ? [...(declared && status === 'present' ? [button('Fetch', 'fetch', { name: r.name })] : []), button('Open in terminal', 'open_terminal', { name: r.name })] : []),
      ...(declared ? [button('Remove from list', 'remove', { name: r.name, target_folder: target(c, r) }), ...((count || protectedNames.has(r.name)) ? [button('Remove anyway…', 'remove_anyway', { name: r.name, target_folder: target(c, r) })] : [])] : []),
    )
    return { id: r.name, folder: r.folder, state, primary: r.primary ? 'Primary' : '' }
  })
  const form = { type: 'form', action: 'prepare_add', submitLabel: 'Review repo', schema: { type: 'object', required: ['remote'], properties: { remote: { type: 'string', title: 'Remote URL' }, folder: { type: 'string', title: 'Folder name', description: 'Leave blank to use the remote name.' }, branch: { type: 'string', title: 'Default branch', default: 'main' }, primary: { type: 'boolean', title: 'Primary repo', default: false } } } }
  const draft = s.drafts[c.viewer]
  const page = { type: 'tabs', id: 'repos', tabs: [
    { id: 'structure', label: 'Structure', node: stack(kv({ 'Workspace root': rootOf(c) }), { type: 'stack', direction: 'row', fit: true, children: [button('Clone all missing', 'clone_all', { targets: plan(s, c) }), button('Fetch all', 'fetch_all')] }, { type: 'table', columns: [{ key: 'folder', label: 'Subfolder' }, { key: 'state', label: 'State', cell: 'state' }, { key: 'primary', label: '', hideBelow: 900 }], rows, rowDetail: { key: 'id', nodes: details } }, { type: 'fold', label: 'Add repo', node: stack(form, ...(draft ? [kv({ Remote: draft.remote, Folder: target(c, draft) }), button('Sign and add', 'add', { ...draft, target_folder: target(c, draft) })] : [])) }) },
    { id: 'checks', label: 'Checks', node: stack(kv({ 'Last check': s.checked }), button('Check now', 'check'), { type: 'list', items: issues, empty: 'All repos are ready.' }) },
    { id: 'activity', label: 'Activity log', node: { type: 'list', items: s.log.map((e, i) => ({ id: `log-${i}`, title: e.text, subtitle: `${e.at} · ${c.store.workspaces.find(w => w.id === c.ws)?.members.find(m => m.person === e.by)?.name ?? e.by} · ${e.type}` })), empty: 'No repo activity yet.' } },
  ] }
  const byTicket: Record<string, unknown> = {}
  for (const t of tickets) {
    if (!canSeeTicket(c, t.key) || !t.links.repos.length) continue
    byTicket[t.key] = stack(...t.links.repos.map(name => {
      const r = s.declared.find(x => x.name === name)
      const o = r ? s.observed[r.folder] : undefined
      return stack({ type: 'list', items: [{ title: name, badge: r ? repoStatus(r, o) : 'not declared', subtitle: o?.branch ?? 'No current branch' }] }, repoLink(name))
    }))
  }
  return { page, byTicket, drafts: {}, moving: Object.values(s.jobs).some(running), glance: { type: 'stat', label: 'repos ready', value: `${rows.filter(r => r.state === 'present').length} of ${s.declared.length}`, hint: `${s.declared.filter(r => !s.observed[r.folder]).length} missing · ${s.declared.filter(r => s.observed[r.folder]?.behind).length} behind` }, settingsPanel: stack(kv({ 'Workspace root': rootOf(c) }), { type: 'form', action: 'save_settings', submitLabel: 'Save settings', formData: s.settings, schema: { type: 'object', properties: { interval: { type: 'string', title: 'Auto-check interval', enum: ['off', '15 min', '1 h', 'daily'] }, fetch: { type: 'boolean', title: 'Fetch on check' } } } }) }
}
function remove(c: AddonCtx, anyway: boolean) {
  const s = stateOf(c.state)
  const r = s.declared.find(r => r.name === c.body.name)
  if (!r) return notFound('No such declared repo.')
  if (c.body.target_folder !== target(c, r)) return conflict('repos.changed', 'The repo folder changed. Review it again.')
  const count = protectedCount(c, r.name)
  if (count && !anyway) return conflict('repos.linked', `${count} open tickets link this repo. An owner can choose Remove anyway.`)
  if (running(s.jobs[r.name])) return conflict('repos.cloning', 'Wait for the clone to finish before removing its declaration.')
  s.declared = s.declared.filter(x => x !== r)
  record(s, c, 'removed', `Removed ${r.name} from the list. The folder and its files stay on disk.`)
  return ok('Removed the declaration. The folder and its files stay on disk.')
}
registerAddon({
  name: 'repos', seed: seedRepos, seedBusy: (ws, store) => seedRepos(ws, store, true),
  view: (s, c) => view(stateOf(s), c),
  decisions(state, _pkg, c) {
    const s = stateOf(state)
    tick(s, c)
    return missing(s).map(r => ({ kind: 'decision', id: `repos.clone.${r.name}`, addon: 'repos', title: `${r.name} is not cloned yet`, question: `Clone ${r.name}?`, options: [{ key: 'clone', label: 'Clone', primary: true }], action: 'clone_attention', terms: cloneArgs(c, r) } satisfies AddonDecision))
  },
  actions: {
    check(c) { const s = stateOf(c.state); tick(s, c); check(s, c); return ok('Repos checked.') },
    clone(c) {
      const s = stateOf(c.state); tick(s, c)
      const r = s.declared.find(r => r.name === c.body.name)
      if (!r) return notFound('No such declared repo.')
      if (Object.entries(cloneArgs(c, r)).some(([k, v]) => c.body[k] !== v)) return conflict('repos.changed', 'The clone target changed. Review it again.')
      if (s.disk[r.folder] || running(s.jobs[r.name])) return conflict('repos.exists', 'That folder exists or is already cloning.')
      queue(s, c, r); return ok('Clone queued.')
    },
    clone_attention(c) {
      if (!c.decision) return conflict('decision.closed', 'That decision is no longer open.')
      const s = stateOf(c.state)
      const r = s.declared.find(r => r.name === c.decision?.terms?.name)
      if (!r) return notFound('No such declared repo.')
      if (s.disk[r.folder] || running(s.jobs[r.name])) return conflict('repos.exists', 'That folder exists or is already cloning.')
      queue(s, c, r); return ok('Clone queued.')
    },
    clone_all(c) {
      const s = stateOf(c.state); tick(s, c)
      if (c.body.targets !== plan(s, c)) return conflict('repos.changed', 'The missing repo list changed. Review the clone targets again.')
      const repos = missing(s)
      if (!repos.length) return conflict('repos.none_missing', 'No missing repos to clone.')
      if (repos.some(r => s.disk[r.folder])) return conflict('repos.exists', 'A target folder now exists. Check again.')
      repos.forEach(r => queue(s, c, r)); return ok('All missing repos queued.')
    },
    fetch(c) {
      const s = stateOf(c.state); const r = s.declared.find(r => r.name === c.body.name)
      if (!r) return notFound('No such declared repo.')
      if (repoStatus(r, s.disk[r.folder]) !== 'present') return conflict('repos.not_ready', 'Check the repo and its remote before fetching.')
      fetchRepos(s, c, [r]); return ok('Fetched repo.')
    },
    fetch_all(c) { const s = stateOf(c.state); fetchRepos(s, c, s.declared); return ok('Fetched ready repos.') },
    prepare_add(c) {
      const s = stateOf(c.state); const r = validateRepo(s, (c.body.formData ?? {}) as Record<string, unknown>)
      if ('ok' in r) return r
      s.drafts[c.viewer] = r; return ok('Review the repo, then sign to add it.')
    },
    add(c) {
      const s = stateOf(c.state); const r = validateRepo(s, c.body)
      if ('ok' in r) return r
      if (c.body.name !== r.name || c.body.target_folder !== target(c, r)) return conflict('repos.changed', 'Review the exact repo name and target folder before signing.')
      add(s, c, r); delete s.drafts[c.viewer]; return ok('Repo added to the declared list.')
    },
    adopt(c) {
      const s = stateOf(c.state); const o = s.observed[String(c.body.folder)]
      if (!o?.git || !s.disk[String(c.body.folder)]?.git || s.disk[String(c.body.folder)]?.remote !== o.remote) return conflict('repos.changed', 'Check this folder again before adopting it.')
      const r = validateRepo(s, { ...c.body, remote: o.remote }, true)
      if ('ok' in r) return r
      if (c.body.remote !== o.remote || c.body.name !== r.name || c.body.target_folder !== target(c, r)) return conflict('repos.changed', 'The observed remote or folder changed. Review it again.')
      add(s, c, r); return ok('Adopted the untracked repo.')
    },
    remove: c => remove(c, false),
    remove_anyway(c) { if (c.body.choice !== 'remove') return invalid('Choose Remove anyway to remove this declaration.'); return remove(c, true) },
    open_terminal(c) {
      const a = c.store.workspaces.find(w => w.id === c.ws)?.addons.repos
      if (!a || !a.capabilities.includes('pty') || !grantCovers(a, a.granted)) return conflict('repos.no_pty', 'Repos needs a current pty grant.')
      const s = stateOf(c.state)
      const folder = s.declared.find(r => r.name === c.body.name)?.folder ?? String(c.body.name)
      if (!s.observed[folder]?.git) return conflict('repos.not_ready', 'This repo is not present on disk.')
      return openRepoShell(c, `${rootOf(c)}/${folder}`)
    },
    save_settings(c) {
      const f = (c.body.formData ?? {}) as Record<string, unknown>
      if (!['off', '15 min', '1 h', 'daily'].includes(String(f.interval)) || typeof f.fetch !== 'boolean') return invalid('Choose a valid check interval and fetch toggle.')
      stateOf(c.state).settings = { interval: f.interval as string, fetch: f.fetch }; return ok('Repo settings saved.')
    },
  },
})
