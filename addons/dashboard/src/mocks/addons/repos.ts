// Repos (preview): the workspace's repos as one folder tree. The DECLARED list is the workspace's own `settings.repos`
// (ticket format §2, owner-signed `settings.changed`): this addon keeps no second list. Its state holds only what it
// observed (the last check), clone jobs, an activity log, its settings and an owner's unsigned draft. `disk` stands in
// for the host's file system (the mock's world, not a list anyone declares). Git never runs: everything is simulated.
import { grantCovers } from '@/api/addons'
import { atLeast } from '@/api/permissions'
import { REPO_NAME, remoteIdentity, remoteProblem, repoFolderProblem, resolveRepoPath } from '@/api/repos'
import type { AddonDecision, Role } from '@/api/types'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, notFound, registerAddon, type AddonCtx } from './registry'
import { openRepoShell } from './terminals'

/** One `settings.repos` entry as this addon reads it. `remote`/`default_branch` are the proposed format amendment. */
export interface Repo {
  name: string
  path: string
  /** `path` resolved against the workspace folder: where the working copy is (or a clone goes). */
  full: string
  remote?: string
  branch: string
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
/** A queued clone carries the exact spec that was signed; it never re-reads the declaration when it runs. */
interface Job { at: string; by: string; attempt: number; error?: string; done?: boolean; spec: { name: string; remote: string; full: string; branch: string; clone_as: string; rev: number } }
interface Draft { name: string; path: string; remote: string; branch: string }
export interface ReposState extends Record<string, unknown> {
  /** The simulated file system, by full path. */
  disk: Record<string, Observed>
  /** The last check's snapshot of `disk`, by full path. */
  observed: Record<string, Observed>
  jobs: Record<string, Job>
  log: { type: string; at: string; by: string; text: string }[]
  checked: string
  settings: { interval: string; fetch: boolean; connection: string }
  drafts: Record<string, Draft>
}
type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
const INTERVALS = ['off', '15 min', '1 h', 'daily']
const INTERVAL_MS: Record<string, number> = { '15 min': 900_000, '1 h': 3_600_000, daily: 86_400_000 }
const stateOf = (s: Record<string, unknown>) => s as ReposState
const wsOf = (c: Pick<Ctx, 'store' | 'ws'>) => c.store.workspaces.find((w) => w.id === c.ws)
const rootOf = (c: Pick<Ctx, 'store' | 'ws'>) => wsOf(c)?.root_folder ?? '~/work/workspace'
const roleOf = (c: Ctx): Role | undefined => c.store.roleIn(c.ws, c.viewer)

/** The declared repos: the workspace's `settings.repos`, by name. */
export function declaredOf(c: Pick<Ctx, 'store' | 'ws'>): Repo[] {
  const root = rootOf(c)
  return Object.entries(wsOf(c)?.repos ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, e]) => ({ name, path: e.path, full: resolveRepoPath(root, e.path), ...(e.remote ? { remote: e.remote } : {}), branch: e.default_branch ?? 'main' }))
}

const sameRemote = (a: string, b: string) => {
  try {
    return remoteIdentity(a) === remoteIdentity(b)
  } catch {
    return a === b
  }
}
export const repoStatus = (repo: Pick<Repo, 'remote'> | undefined, o: Observed | undefined) =>
  !repo ? 'untracked' : !o ? 'missing' : !o.git ? 'not a repo' : repo.remote && !sameRemote(repo.remote, o.remote) ? 'remote differs' : 'present'

const fake = (name: string) => `https://git.example.test/acme/${name}.git`
const observation = (remote: string, at = '2026-10-09T10:00:00Z'): Observed => ({ git: true, remote, branch: 'main', ahead: 0, behind: 0, dirty: 0, fetched: at, size: '128 MB', worktrees: 1 })

export function seedRepos(ws: string, store: MockStore): ReposState {
  const c = { store, ws }
  const disk: Record<string, Observed> = {}
  declaredOf(c).forEach((r, i) => {
    if (r.name === 'billing-api' || r.name === 'private-api') return // missing (private-api's first clone fails)
    const o = observation(r.remote ?? fake(r.name))
    if (r.name === 'meter-ingest') o.behind = 3
    if (r.name === 'web-portal') o.dirty = 2
    if (r.name === 'docs-site') o.remote = fake('old-docs')
    if (['analytics', 'search', 'reports'].includes(r.name)) o.behind = 1 + (i % 3)
    disk[r.full] = o
  })
  if (wsOf(c)?.prefix === 'DEMO') disk[resolveRepoPath(rootOf(c), 'sandbox')] = observation(fake('sandbox'))
  return { disk, observed: structuredClone(disk), jobs: {}, log: [], checked: store.now(), settings: { interval: 'off', fetch: false, connection: 'gh' }, drafts: {} }
}

const ok = (message: string) => ({ ok: true as const, message, changed: true })
type Verb = 'declared' | 'removed' | 'checked' | 'clone_queued' | 'cloned' | 'clone_failed' | 'clone_cancelled' | 'fetched'
/**
 * The activity log, and the addon's own `repos.<verb>` record (actor addon:repos). A declaration change is core's
 * `settings.changed`, signed by the owner, so `declared`/`removed` go only into this log, not into a second event.
 */
function record(s: ReposState, c: Ctx, verb: Verb, text: string, by = c.viewer) {
  s.log.unshift({ type: `repos.${verb}`, at: c.store.now(), by, text })
  s.log = s.log.slice(0, 200)
  if (verb !== 'declared' && verb !== 'removed') c.store.appendWs(c.ws, { type: `repos.${verb}` as 'repos.checked', actor: { kind: 'addon', id: 'repos' }, person: by, text })
}

/**
 * The git login clone and fetch run with (D56 A): a CLI login of the user running orch, declared as a connection (D55).
 * orch stores no git credentials; on the dev machine this is the workspace bot account.
 */
function cloneAs(s: ReposState, c: Ctx) {
  const conn = gitLogins(c).find((x) => x.name === s.settings.connection)
  if (!conn) return null
  // The whole identity is signed and snapshotted (connection, tool, account incl. host, OS user), not just a name.
  return { name: conn.name, account: conn.target.value, runAs: conn.run_as, identity: `${conn.name} · tool ${conn.tool} · ${conn.target.value} · OS user ${conn.run_as}` }
}
/**
 * How many declaration changes touched this repo name so far (core's settings.changed events). A queued clone keeps the
 * number it was signed at: any later change (even one that restores the same values) cancels it.
 */
const declRev = (c: Ctx, name: string) =>
  c.store.wsEventsOf(c.ws).filter((e) => e.type === 'settings.changed' && Object.hasOwn((e.set as { repos?: object } | undefined)?.repos ?? {}, name)).length
/** Connections that are a git CLI login (gh, glab, git): the only ones clone and fetch may run as. */
const gitLogins = (c: Ctx) => c.store.conn.connections(c.ws).filter((x) => x.kind === 'cli_login' && ['gh', 'glab', 'git'].includes(x.tool))
const loginChoices = (c: Ctx) => gitLogins(c).map((x) => x.name)

/** Open tickets this viewer can see that link the repo (restricted tickets they cannot see are never counted). */
const linkedOpen = (c: Ctx, name: string) => c.store.listTickets(c.ws).filter((t) => t.status !== 'done' && t.links.repos.includes(name) && canSeeTicket(c, t.key))

const cloneArgs = (s: ReposState, c: Ctx, r: Repo) => ({ name: r.name, remote: r.remote ?? '', default_branch: r.branch, target_folder: r.full, clone_as: cloneAs(s, c)?.identity ?? '' })
const running = (job?: Job) => !!job && !job.done && !job.error
const missing = (s: ReposState, c: Pick<Ctx, 'store' | 'ws'>) => declaredOf(c).filter((r) => r.remote && !s.observed[r.full] && !running(s.jobs[r.name]))
const plan = (s: ReposState, c: Ctx) => missing(s, c).map((r) => `${r.remote} @ ${r.branch} → ${r.full}`).join('\n')

function tick(s: ReposState, c: Ctx) {
  const now = Date.parse(c.store.now())
  const declared = new Map(declaredOf(c).map((r) => [r.name, r]))
  for (const [name, j] of Object.entries(s.jobs)) {
    if (!running(j)) continue
    // Chosen rule (U3 round 2): a declaration change or removal after signing cancels the queued clone. The clone only
    // ever runs the signed spec, and only while the declaration still says exactly that.
    const r = declared.get(name)
    if (!r || declRev(c, name) !== j.spec.rev || r.remote !== j.spec.remote || r.full !== j.spec.full || r.branch !== j.spec.branch || cloneAs(s, c)?.identity !== j.spec.clone_as) {
      j.error = 'The declaration or git login changed after it was signed; the clone was cancelled.'
      record(s, c, 'clone_cancelled', `${name}: clone cancelled, the declaration changed after signing.`, j.by)
      continue
    }
    if (now - Date.parse(j.at) < 6000) continue
    if (name === 'private-api' && j.attempt === 1) {
      j.error = 'Repository not found or no access'
      record(s, c, 'clone_failed', `${name}: ${j.error}`, j.by)
    } else if (s.disk[j.spec.full]) {
      // Never overwrite a folder that appeared after the clone was queued.
      j.error = 'The target folder exists now. Check before retrying.'
    } else {
      s.disk[j.spec.full] = { ...observation(j.spec.remote, c.store.now()), branch: j.spec.branch }
      s.observed[j.spec.full] = structuredClone(s.disk[j.spec.full])
      j.done = true
      record(s, c, 'cloned', `Cloned ${name} into ${j.spec.full}.`, j.by)
    }
  }
  // The real host schedules this itself; the mock runs a due check on the next read (nextRefreshMs asks for that read).
  const every = INTERVAL_MS[s.settings.interval]
  if (every && now - Date.parse(s.checked) >= every) check(s, c, 'orch')
}
function check(s: ReposState, c: Ctx, by = c.viewer) {
  const fetched = s.settings.fetch ? fetchRepos(s, c, declaredOf(c), by) : true
  s.observed = structuredClone(s.disk)
  s.checked = c.store.now()
  record(s, c, 'checked', fetched ? 'Checked all repo folders.' : 'Checked all repo folders; fetch skipped: no git login is declared as a connection.', by)
}
/** The one fetch operation (every entry point): runs only as a declared git login; false when there is none. */
function fetchRepos(s: ReposState, c: Ctx, repos: Repo[], by = c.viewer): boolean {
  if (!cloneAs(s, c)) return false
  for (const r of repos) {
    const o = s.disk[r.full]
    if (repoStatus(r, o) !== 'present') continue
    // Fetch refreshes tracking information (simulated: one new remote commit); it never pulls or touches local work.
    o.fetched = c.store.now()
    o.behind += 1
    s.observed[r.full] = structuredClone(o)
    record(s, c, 'fetched', `Fetched ${r.name}.`, by)
  }
  return true
}
function queue(s: ReposState, c: Ctx, r: Repo) {
  const spec = { name: r.name, remote: r.remote!, full: r.full, branch: r.branch, clone_as: cloneAs(s, c)!.identity, rev: declRev(c, r.name) }
  s.jobs[r.name] = { at: c.store.now(), by: c.viewer, attempt: (s.jobs[r.name]?.attempt ?? 0) + 1, spec }
  record(s, c, 'clone_queued', `Queued ${r.name}.`)
}
/** Refuses a clone the host cannot run as signed: no remote declared, no git login, or a changed identity. */
function cloneBlocked(s: ReposState, c: Ctx, r: Repo, body: Record<string, unknown>) {
  if (!r.remote) return conflict('repos.no_remote', `${r.name} has no remote in settings.repos, so there is nothing to clone from.`)
  if (!cloneAs(s, c)) return conflict('repos.no_login', 'No git login is declared as a connection.', 'Declare one in Settings > Connections; orch stores no git credentials.')
  if (Object.entries(cloneArgs(s, c, r)).some(([k, v]) => body[k] !== v)) return conflict('repos.changed', 'The clone target or identity changed. Review it again.')
  if (s.disk[r.full] || running(s.jobs[r.name])) return conflict('repos.exists', 'That folder exists or is already cloning.')
  return null
}

/** Host validation of a new declaration (both at draft and at signing). Remote first: credentials never get further. */
function validateNew(s: ReposState, c: Ctx, data: Record<string, unknown>, adopting = false): Draft | ReturnType<typeof invalid> {
  const problem = remoteProblem(data.remote)
  if (problem) return invalid(problem)
  const remote = data.remote as string
  const fromUrl = remote.split(/[/:]/).pop()!.replace(/\.git$/, '')
  const name = data.name === '' || data.name === undefined ? fromUrl : data.name
  if (typeof name !== 'string' || !REPO_NAME.test(name)) return invalid('A repo name is letters, digits, dots, underscores or dashes, starting with a letter or digit (at most 100).')
  const path = data.path === '' || data.path === undefined ? name : data.path
  const folderProblem = repoFolderProblem(path)
  if (folderProblem) return invalid(folderProblem)
  const branch = data.default_branch === '' || data.default_branch === undefined ? 'main' : data.default_branch
  if (typeof branch !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/.test(branch) || branch.includes('..') || branch.endsWith('/')) return invalid('Write a valid default branch.')
  const declared = declaredOf(c)
  const full = resolveRepoPath(rootOf(c), path as string).toLowerCase()
  if (declared.some((r) => r.name.toLowerCase() === name.toLowerCase())) return conflict('repos.duplicate_name', `${name} is already declared.`)
  if (declared.some((r) => r.full.toLowerCase() === full)) return conflict('repos.duplicate_folder', 'Another declared repo uses that folder.')
  if (!adopting && Object.keys(s.disk).some((p) => p.toLowerCase() === full)) return conflict('repos.duplicate_folder', 'That folder already exists.', 'Declare the untracked repo instead.')
  if (declared.some((r) => r.remote && sameRemote(r.remote, remote))) return conflict('repos.duplicate_remote', 'That remote is already declared.')
  return { name, path: path as string, remote, branch }
}
/** Core's owner-signed change of settings.repos (store.changeRepos checks owner and paths again). */
function declare(s: ReposState, c: Ctx, d: Draft, verb: 'Declared' | 'Declared untracked') {
  const res = c.store.changeRepos(c.ws, { [d.name]: { path: d.path, remote: d.remote, default_branch: d.branch } }, { kind: 'person', id: c.viewer })
  if (!res.ok) return res
  record(s, c, 'declared', `${verb} ${d.name} at ${d.path}.`)
  return ok(`${d.name} is declared in the workspace settings.`)
}

const button = (label: string, action: string, args?: Record<string, string | boolean>) => ({ type: 'button', label, action, ...(args ? { args } : {}) })
const stack = (...children: unknown[]) => ({ type: 'stack', children })
const kv = (pairs: Record<string, string | number>) => ({ type: 'kv', pairs: Object.entries(pairs).map(([label, value]) => ({ label, value })) })
/** A permanent link to one repo's row (core opens it: `?row=`). */
const rowLink = (name: string) => ({ type: 'link', label: `Open ${name} in Repos`, href: `/addon/repos/repos?tab.repos=structure&row=${name}` })

function view(s: ReposState, c: Ctx) {
  tick(s, c)
  const role = roleOf(c)
  const owner = role === 'owner'
  const declared = declaredOf(c)
  const known = new Set(declared.map((r) => r.full))
  const untracked = Object.keys(s.observed).filter((p) => s.observed[p].git && !known.has(p))
  const identity = cloneAs(s, c)
  const details: Record<string, unknown> = {}
  const issues: { title: string; id: string }[] = []
  const declaredRows = declared.map((r) => {
    const o = s.observed[r.full]
    const status = repoStatus(r, o)
    const job = s.jobs[r.name]
    const elapsed = job ? Date.parse(c.store.now()) - Date.parse(job.at) : 0
    const state = running(job) ? (elapsed < 1000 ? 'queued' : `cloning ${Math.min(95, Math.floor(elapsed / 60))}%`) : job?.error ? 'clone failed' : status
    if (status === 'missing') issues.push({ id: r.name, title: `${r.name}: missing — ${r.remote ? 'not cloned yet' : 'no remote declared to clone from'}` })
    else if (status === 'remote differs') issues.push({ id: r.name, title: `${r.name}: remote differs: expected ${r.remote}, found ${o!.remote}` })
    else if (status !== 'present') issues.push({ id: r.name, title: `${r.name}: ${status}` })
    if (o?.behind) issues.push({ id: `${r.name}-behind`, title: `${r.name}: ${o.behind} behind origin/${r.branch}` })
    if (o?.dirty) issues.push({ id: `${r.name}-dirty`, title: `${r.name}: ${o.dirty} uncommitted changes` })
    const count = linkedOpen(c, r.name).length
    details[r.name] = stack(
      !r.remote ? kv({ 'Remote URL': 'Not declared (settings.repos has only the path)' }) : /^https:\/\//.test(r.remote) ? { type: 'link', label: 'Remote URL', href: r.remote, copy: true } : kv({ 'Remote URL': r.remote }),
      kv({
        Path: r.full,
        'Default branch': r.branch,
        'Current branch': o?.branch ?? '—',
        'Ahead / behind': o ? `${o.ahead} / ${o.behind}` : '—',
        'Uncommitted changes': o?.dirty ?? '—',
        'Last fetch': o?.fetched ?? 'Never',
        'Size on disk': o?.size ?? '—',
        Worktrees: o?.worktrees ?? 0,
        'Linked open tickets': count,
        ...(status === 'remote differs' ? { 'Observed remote': o!.remote } : {}),
      }),
      { type: 'link', label: `Tickets linking ${r.name}`, href: `/tickets?repo=${r.name}` },
      ...(job?.error ? [{ type: 'alert', tone: 'error', title: job.error }] : []),
      ...(status === 'missing' && r.remote && !running(job) && atLeast(role, 'maintainer') ? [button(job?.error ? 'Retry clone' : 'Clone', 'clone', cloneArgs(s, c, r))] : []),
      ...(status === 'present' && atLeast(role, 'member') ? [button('Fetch', 'fetch', { name: r.name }), button('Open in terminal', 'open_terminal', { name: r.name })] : []),
      ...(owner ? [count ? button('Remove anyway…', 'remove_anyway', { name: r.name, target_folder: r.full }) : button('Remove from declared repos', 'remove', { name: r.name, target_folder: r.full })] : []),
    )
    return { id: r.name, repo: r.name, path: r.path, state }
  })
  const untrackedRows = untracked.map((p) => {
    const base = p.split('/').pop()!
    const id = `untracked-${base}`
    issues.push({ id, title: `${base}: untracked git repo in the workspace folder` })
    details[id] = stack(
      kv({ 'Observed remote': s.observed[p].remote, Path: p, 'Current branch': s.observed[p].branch }),
      // Every value that would be declared is a signed arg, the observed branch included (Codex integration review #2).
      ...(owner && REPO_NAME.test(base) ? [button('Declare untracked repo', 'adopt', { name: base, path: base, remote: s.observed[p].remote, default_branch: s.observed[p].branch, target_folder: p })] : []),
    )
    return { id, repo: base, path: base, state: 'untracked' }
  })
  const draft = s.drafts[c.viewer]
  const addForm = {
    type: 'form',
    action: 'prepare_add',
    submitLabel: 'Review repo',
    schema: {
      type: 'object',
      required: ['remote'],
      properties: {
        remote: { type: 'string', title: 'Remote URL', description: 'HTTPS, ssh:// or git@host:path. No credentials.' },
        name: { type: 'string', title: 'Repo name', description: 'Leave blank to use the remote name.' },
        path: { type: 'string', title: 'Folder', description: 'One folder in the workspace folder. Blank: the repo name.' },
        default_branch: { type: 'string', title: 'Default branch', default: 'main' },
      },
    },
  }
  const add = owner
    ? { type: 'fold', label: 'Declare a repo', node: stack(addForm, ...(draft ? [kv({ Name: draft.name, Remote: draft.remote, Folder: resolveRepoPath(rootOf(c), draft.path), 'Default branch': draft.branch }), button('Sign and declare', 'add', { name: draft.name, path: draft.path, remote: draft.remote, default_branch: draft.branch, target_folder: resolveRepoPath(rootOf(c), draft.path) })] : [])) }
    : { type: 'markdown', text: 'Only owners change the declared repos (the workspace settings).' }
  const page = {
    type: 'tabs',
    id: 'repos',
    tabs: [
      {
        id: 'structure',
        label: 'Structure',
        node: stack(
          kv({ 'Workspace folder': rootOf(c), 'Clones as': identity ? identity.identity : 'No git login declared as a connection' }),
          ...(atLeast(role, 'member') ? [{ type: 'stack', direction: 'row', fit: true, children: [...(atLeast(role, 'maintainer') && missing(s, c).length ? [button('Clone all missing', 'clone_all', { targets: plan(s, c), clone_as: identity?.identity ?? '' })] : []), button('Fetch all', 'fetch_all')] }] : []),
          { type: 'table', columns: [{ key: 'repo', label: 'Repo' }, { key: 'path', label: 'Folder', hideBelow: 600 }, { key: 'state', label: 'State', cell: 'state' }], rows: [...declaredRows, ...untrackedRows], rowDetail: { key: 'id', nodes: details } },
          add,
        ),
      },
      { id: 'checks', label: 'Checks', node: stack(kv({ 'Last check': s.checked }), ...(atLeast(role, 'member') ? [button('Check now', 'check')] : []), { type: 'list', items: issues, empty: 'All repos are ready.' }) },
      { id: 'activity', label: 'Activity log', node: { type: 'list', items: s.log.map((e, i) => ({ id: `log-${i}`, title: e.text, subtitle: `${e.at} · ${wsOf(c)?.members.find((m) => m.person === e.by)?.name ?? e.by} · ${e.type}` })), empty: 'No repo activity yet.' } },
    ],
  }
  const byTicket: Record<string, unknown> = {}
  for (const t of c.store.listTickets(c.ws)) {
    if (!canSeeTicket(c, t.key) || !t.links.repos.length) continue
    byTicket[t.key] = stack(
      ...t.links.repos.map((name) => {
        const r = declared.find((x) => x.name === name)
        const o = r ? s.observed[r.full] : undefined
        return stack({ type: 'list', items: [{ title: name, badge: r ? repoStatus(r, o) : 'not declared', subtitle: o ? `Checked out: ${o.branch}` : 'Not on disk' }] }, ...(r ? [rowLink(name)] : []))
      }),
    )
  }
  const ready = declaredRows.filter((r) => r.state === 'present').length
  return {
    page,
    byTicket,
    drafts: {},
    moving: Object.values(s.jobs).some(running),
    // Generic contract with core's shared query: read again when the next scheduled check is due (core bounds it).
    ...(INTERVAL_MS[s.settings.interval] ? { nextRefreshMs: Math.max(0, Date.parse(s.checked) + INTERVAL_MS[s.settings.interval] - Date.parse(c.store.now())) } : {}),
    glance: { type: 'stat', label: 'repos ready', value: `${ready} of ${declared.length}`, hint: `${declared.filter((r) => !s.observed[r.full]).length} missing · ${declared.filter((r) => s.observed[r.full]?.behind).length} behind` },
    settingsPanel: stack(
      kv({ 'Workspace folder': rootOf(c) }),
      {
        type: 'form',
        action: 'save_settings',
        submitLabel: 'Save settings',
        formData: s.settings,
        schema: { type: 'object', properties: { interval: { type: 'string', title: 'Auto-check interval', enum: INTERVALS }, fetch: { type: 'boolean', title: 'Fetch on check' }, connection: { type: 'string', title: 'Git login (connection)', enum: loginChoices(c).length ? loginChoices(c) : [s.settings.connection] } } },
      },
    ),
  }
}

function remove(c: AddonCtx, anyway: boolean) {
  const s = stateOf(c.state)
  const r = declaredOf(c).find((x) => x.name === c.body.name)
  if (!r) return notFound('No such declared repo.')
  if (c.body.target_folder !== r.full) return conflict('repos.changed', 'The repo folder changed. Review it again.')
  const count = linkedOpen(c, r.name).length
  if (count && !anyway) return conflict('repos.linked', `${count} open ${count === 1 ? 'ticket links' : 'tickets link'} this repo.`, 'An owner can choose Remove anyway.')
  if (running(s.jobs[r.name])) return conflict('repos.cloning', 'Wait for the clone to finish before removing its declaration.')
  const res = c.store.changeRepos(c.ws, { [r.name]: null }, { kind: 'person', id: c.viewer })
  if (!res.ok) return res
  record(s, c, 'removed', `Removed ${r.name} from the declared repos. The folder and its files stay on disk.`)
  return ok('Removed the declaration. The folder and its files stay on disk.')
}

registerAddon({
  name: 'repos',
  stateVersion: 3,
  seed: seedRepos,
  view: (s, c) => view(stateOf(s), c),
  decisions(state, _pkg, c) {
    const s = stateOf(state)
    tick(s, c)
    if (!cloneAs(s, c)) return []
    return missing(s, c).map((r) => ({ kind: 'decision', id: `repos.clone.${r.name}`, addon: 'repos', title: `${r.name} is not cloned yet`, question: `Clone ${r.name}?`, options: [{ key: 'clone', label: 'Clone', primary: true }], action: 'clone_attention', terms: cloneArgs(s, c, r) }) satisfies AddonDecision)
  },
  actions: {
    check(c) {
      const s = stateOf(c.state)
      tick(s, c)
      check(s, c)
      return ok('Repos checked.')
    },
    clone(c) {
      const s = stateOf(c.state)
      tick(s, c)
      const r = declaredOf(c).find((x) => x.name === c.body.name)
      if (!r) return notFound('No such declared repo.')
      const blocked = cloneBlocked(s, c, r, c.body)
      if (blocked) return blocked
      queue(s, c, r)
      return ok('Clone queued.')
    },
    clone_attention(c) {
      if (!c.decision) return conflict('decision.closed', 'That decision is no longer open.')
      const s = stateOf(c.state)
      const r = declaredOf(c).find((x) => x.name === c.decision?.terms?.name)
      if (!r) return notFound('No such declared repo.')
      const blocked = cloneBlocked(s, c, r, c.decision?.terms ?? {})
      if (blocked) return blocked
      queue(s, c, r)
      return ok('Clone queued.')
    },
    clone_all(c) {
      const s = stateOf(c.state)
      tick(s, c)
      const repos = missing(s, c)
      if (!repos.length) return conflict('repos.none_missing', 'No missing repos to clone.')
      if (c.body.targets !== plan(s, c) || c.body.clone_as !== (cloneAs(s, c)?.identity ?? '')) return conflict('repos.changed', 'The missing repos or the git login changed. Review the clone targets again.')
      for (const r of repos) {
        const blocked = cloneBlocked(s, c, r, cloneArgs(s, c, r))
        if (blocked) return blocked
      }
      repos.forEach((r) => queue(s, c, r))
      return ok('All missing repos queued.')
    },
    fetch(c) {
      const s = stateOf(c.state)
      const r = declaredOf(c).find((x) => x.name === c.body.name)
      if (!r) return notFound('No such declared repo.')
      if (repoStatus(r, s.disk[r.full]) !== 'present') return conflict('repos.not_ready', 'Check the repo and its remote before fetching.')
      if (!cloneAs(s, c)) return conflict('repos.no_login', 'No git login is declared as a connection.')
      fetchRepos(s, c, [r])
      return ok('Fetched repo.')
    },
    fetch_all(c) {
      const s = stateOf(c.state)
      if (!cloneAs(s, c)) return conflict('repos.no_login', 'No git login is declared as a connection.')
      fetchRepos(s, c, declaredOf(c))
      return ok('Fetched ready repos.')
    },
    prepare_add(c) {
      const s = stateOf(c.state)
      const d = validateNew(s, c, (c.body.formData ?? {}) as Record<string, unknown>)
      if ('ok' in d) return d
      s.drafts[c.viewer] = d
      return ok('Review the repo, then sign to declare it.')
    },
    add(c) {
      const s = stateOf(c.state)
      const d = validateNew(s, c, c.body)
      if ('ok' in d) return d
      if (c.body.name !== d.name || c.body.path !== d.path || c.body.default_branch !== d.branch || c.body.target_folder !== resolveRepoPath(rootOf(c), d.path))
        return conflict('repos.changed', 'Review the exact repo name, folder and branch before signing.')
      const res = declare(s, c, d, 'Declared')
      if (res.ok) delete s.drafts[c.viewer]
      return res
    },
    adopt(c) {
      const s = stateOf(c.state)
      const full = resolveRepoPath(rootOf(c), String(c.body.path ?? ''))
      const o = s.observed[full]
      if (!o?.git || !s.disk[full]?.git || s.disk[full].remote !== o.remote || c.body.target_folder !== full) return conflict('repos.changed', 'Check this folder again before declaring it.')
      if (c.body.remote !== o.remote) return conflict('repos.changed', 'The observed remote changed. Review it again.')
      if (c.body.default_branch !== o.branch) return conflict('repos.changed', 'The observed branch changed. Review it again.')
      const d = validateNew(s, c, { name: c.body.name, path: c.body.path, remote: o.remote, default_branch: o.branch }, true)
      if ('ok' in d) return d
      return declare(s, c, d, 'Declared untracked')
    },
    remove: (c) => remove(c, false),
    remove_anyway(c) {
      if (c.body.choice !== 'remove') return invalid('Choose Remove anyway to remove this declaration.')
      return remove(c, true)
    },
    open_terminal(c) {
      const a = wsOf(c)?.addons.repos
      if (!a || !a.capabilities.includes('pty') || !grantCovers(a, a.granted)) return conflict('repos.no_pty', 'Repos needs a current pty grant.')
      const s = stateOf(c.state)
      const r = declaredOf(c).find((x) => x.name === c.body.name)
      if (!r) return notFound('No such declared repo.')
      if (!s.observed[r.full]?.git) return conflict('repos.not_ready', 'This repo is not present on disk.')
      return openRepoShell(c, r.full)
    },
    save_settings(c) {
      const f = (c.body.formData ?? {}) as Record<string, unknown>
      const s = stateOf(c.state)
      const connection = f.connection === undefined ? s.settings.connection : f.connection
      if (!INTERVALS.includes(String(f.interval)) || typeof f.fetch !== 'boolean') return invalid('Choose a valid check interval and fetch toggle.')
      if (typeof connection !== 'string' || !loginChoices(c).includes(connection)) return invalid('Choose a git login declared as a connection.')
      s.settings = { interval: f.interval as string, fetch: f.fetch, connection }
      return ok('Repo settings saved.')
    },
  },
})
