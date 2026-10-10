import type { AddonDecision } from '@/api/types'
import { briefs, tokenOf } from '../busy/helpers'
import type { Rng } from '../busy/rng'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, invalid, markDecided, notFound, registerAddon, type AddonCtx } from './registry'

// publish: apps served from the workspace and read-only shares. Addon state is the single source of truth for both;
// nothing is written to ticket addon data. The ticket panel reads `addon.sharesByTicket.$ticket` (see view()).

interface App {
  id: string
  name: string
  kind: 'streamlit' | 'static' | 'notebook'
  folder: string
  status: 'running' | 'stopped' | 'failed'
  recipients: number
  log: string[]
  /** Simulated host numbers for the row's details (see appStats); seeded, changed by start/stop/redeploy. */
  stats?: AppStats
}
interface AppStats {
  /** When the app got its current status. */
  since: string
  deploy: { commit: string; at: string; ok: boolean }
  /** CPU % and memory while running. */
  cpu: number
  mem_mb: number
  mem_quota_mb: number
  disk_mb: number
  disk_quota_mb: number
  /** Requests per hour over the last 24 hours, oldest first. */
  requests: number[]
}
type ShareKind = 'public link' | 'secret link' | 'sealed' | 'show-once'
interface Share {
  id: string
  ticket?: string
  title: string
  kind: ShareKind
  /** Person a sealed share is for. */
  recipient?: string
  expires_in_days: number
  views: number
  last_viewer: string | null
  /** Most views before the link stops (show-once links choose it; absent = no limit). */
  view_limit?: number
  /** Token of the link; null for a show-once link (only ever shown in core's "Copy this link now" dialog) and for a sealed share. */
  token: string | null
}
interface Settings {
  default_expiry_days: number
  namespace: string
  allow_artifacts?: boolean
}

const seedApps = (): App[] => [
  { id: 'app_billing', name: 'Billing explorer', kind: 'streamlit', folder: 'apps/billing-explorer', status: 'running', recipients: 2, log: ['streamlit run app.py --server.port 8501', 'You can now view your Streamlit app', 'Serving to Mara and Severin'] },
  { id: 'app_energy', name: 'Energy dashboard', kind: 'static', folder: 'apps/energy-dashboard', status: 'stopped', recipients: 0, log: ['Built 14 files in 0.8 s', 'Stopped by Severin'] },
  {
    id: 'app_ops',
    name: 'Ops notebook',
    kind: 'notebook',
    folder: 'apps/ops-notebook',
    status: 'failed',
    recipients: 0,
    log: ['Installing requirements.txt', 'ERROR: pandas==2.3.1 requires numpy>=2.0', 'Traceback (most recent call last):', "ModuleNotFoundError: No module named 'meter_utils'", 'Build failed (exit 1)'],
  },
]
/** The mock's "now" (store MOCK_EPOCH), which the seeded app times count back from. */
const SEED_NOW = Date.parse('2026-10-09T11:30:00Z')

/**
 * Seeded, stable host numbers for an app (mock only): disk, memory, CPU, the last deploy and 24 hours of requests
 * (an app that is not running served nothing). `seedIndex` picks the demo apps' fixed "since" times.
 */
function appStats(x: Pick<App, 'id' | 'status'>, seedIndex: number): AppStats {
  let h = 0
  for (const ch of x.id) h = (h * 31 + ch.charCodeAt(0)) % 2147483647
  const rand = () => {
    h = (h * 1103515245 + 12345) % 2147483648
    return h / 2147483648
  }
  const hex = Array.from({ length: 7 }, () => '0123456789abcdef'[Math.floor(rand() * 16)]).join('')
  const sinceH = [27.8, 43.4, 3.3][seedIndex] ?? 2 + Math.floor(rand() * 60)
  const since = new Date(SEED_NOW - sinceH * 3_600_000).toISOString().replace(/:\d{2}\.\d{3}Z$/, ':00Z')
  const deployAt = new Date(Date.parse(since) - 2 * 60_000).toISOString()
  const quota = x.status === 'running' ? 5120 : 2048
  const ranHours = x.status === 'running' ? 24 : 0
  return {
    since,
    deploy: { commit: hex, at: deployAt.replace(/\.\d{3}Z$/, 'Z'), ok: x.status !== 'failed' },
    cpu: 4 + Math.floor(rand() * 30),
    mem_mb: 180 + Math.floor(rand() * 600),
    mem_quota_mb: 1024,
    disk_mb: 300 + Math.floor(rand() * 1700),
    disk_quota_mb: quota,
    requests: Array.from({ length: 24 }, (_, i) => (i >= 24 - ranHours ? Math.round((20 + rand() * 60) * (i % 24 > 8 && i % 24 < 20 ? 1.6 : 0.5)) : 0)),
  }
}
const withStats = (list: App[]): App[] => list.map((x, i) => ({ ...x, stats: x.stats ?? appStats(x, i) }))
const statsOf = (x: App): AppStats => (x.stats ??= appStats(x, -1))

const RECIPIENTS = ['Mara', 'Severin', 'Tom', 'Ida', 'Jonas']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
/** "8 Oct, 07:40 UTC". */
const when = (iso: string) => {
  const d = new Date(iso)
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`
}
/** "1 day 3 h", "3 h 18 min", "12 min". */
const span = (ms: number) => {
  const min = Math.max(0, Math.round(ms / 60_000))
  const d = Math.floor(min / 1440)
  const hh = Math.floor((min % 1440) / 60)
  if (d) return `${plural(d, 'day', 'days')} ${hh} h`
  return hh ? `${hh} h ${min % 60} min` : `${min} min`
}
const size = (mb: number) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`)
const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
const appUrl = (s: Settings, x: App) => `https://${slug(x.name)}.apps.${s.namespace}.example`

/** The node an app row opens to (table `rowDetail`): its address, host numbers, last deploy and who it is served to. */
function appDetail(x: App, s: Settings, now: string) {
  const st = statsOf(x)
  const running = x.status === 'running'
  const requests = st.requests.reduce((a, b) => a + b, 0)
  const statusWord = { running: 'Running', stopped: 'Stopped', failed: 'Failed' }[x.status]
  return {
    type: 'stack',
    children: [
      { type: 'link', label: `${x.name} address`, href: appUrl(s, x), copy: true },
      {
        type: 'stack',
        direction: 'row',
        children: [
          {
            type: 'kv',
            pairs: [
              { label: 'Status', value: `${statusWord} since ${when(st.since)}` },
              { label: 'Uptime', value: running ? span(Date.parse(now) - Date.parse(st.since)) : '–' },
              { label: 'CPU', value: running ? `${st.cpu} %` : '–' },
              { label: 'Memory', value: running ? `${size(st.mem_mb)} of ${size(st.mem_quota_mb)}` : '–' },
            ],
          },
          {
            type: 'kv',
            pairs: [
              { label: 'Disk', value: `${size(st.disk_mb)} of ${size(st.disk_quota_mb)}` },
              { label: 'Last deploy', value: `${st.deploy.commit} · ${st.deploy.ok ? '' : 'failed · '}${when(st.deploy.at)}` },
              { label: 'Recipients', value: x.recipients ? RECIPIENTS.slice(0, x.recipients).join(', ') : 'nobody yet' },
            ],
          },
          { type: 'stat', label: 'Requests · last 24 h', value: requests.toLocaleString('en-US'), hint: running ? 'per hour' : 'not served now', trend: st.requests },
        ],
      },
    ],
  }
}

const seedShares = (): Share[] => [
  { id: 'sh_report', ticket: 'DEMO-0041', title: 'Before/after report', kind: 'secret link', expires_in_days: 6, views: 14, last_viewer: 'Mara, 2 h ago', token: 'Qm4x9TbA2c' },
  { id: 'sh_utc', ticket: 'DEMO-0042', title: 'UTC migration summary', kind: 'secret link', expires_in_days: 3, views: 5, last_viewer: 'anonymous, yesterday', token: 'Rj7pLw3Yd8' },
  { id: 'sh_dash', title: 'Energy data portal', kind: 'public link', expires_in_days: 30, views: 212, last_viewer: 'anonymous, 12 min ago', token: 'public-energy' },
  { id: 'sh_tariff', ticket: 'DEMO-0041', title: 'Tariff API notes', kind: 'sealed', recipient: 'Mara', expires_in_days: 14, views: 3, last_viewer: 'Mara, 3 days ago', token: null },
  { id: 'sh_recon', ticket: 'DEMO-0041', title: 'Reconciliation table', kind: 'secret link', expires_in_days: 1, views: 2, last_viewer: 'anonymous, 5 days ago', token: 'Zk2nVc6Hs1' },
]

const settingsOf = (state: Record<string, unknown>): Settings => ({ default_expiry_days: 7, namespace: 'acme', ...(state.settings as Partial<Settings> | undefined) })
const apps = (state: Record<string, unknown>) => state.apps as App[]
const shares = (state: Record<string, unknown>) => state.shares as Share[]
const linkOf = (s: Settings, token: string) => `https://p.${s.namespace}.example/s/${token}`
/** A share by id, unless it belongs to a ticket the caller cannot see. */
const visibleShare = (c: AddonCtx, id: unknown) => shares(c.state).find((s) => s.id === id && (!s.ticket || canSeeTicket(c, s.ticket)))
/** The one line that says why a build failed (the last error-looking log line). */
const failedLine = (x: App) => [...x.log].reverse().find((l) => /error/i.test(l)) ?? x.log[x.log.length - 2] ?? 'Build failed'
/** What a show-once link can share, how long it works and how often it opens (the dialog's choices; the host checks them). */
const SHARE_WHAT: Record<string, string> = { ticket: 'ticket page', report: 'before/after report' }
const SHARE_DAYS = [1, 3, 7, 30]
const SHARE_VIEWS = [1, 3, 10, 0]
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** Deterministic 10-character token (mock only), so tests and screenshots are stable. */
function token(state: Record<string, unknown>): string {
  const n = (state.token_seq = ((state.token_seq as number | undefined) ?? 0) + 1)
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  let x = n * 2654435761
  let out = ''
  for (let i = 0; i < 10; i++) {
    x = (x * 1103515245 + 12345) % 2147483648
    out += chars[x % chars.length]
  }
  return out
}

function newShare(state: Record<string, unknown>, ticket: string | undefined, kind: ShareKind, title: string, tok: string | null): Share {
  const s: Share = { id: `sh_${token(state).slice(0, 6)}`, title, kind, expires_in_days: settingsOf(state).default_expiry_days, views: 0, last_viewer: null, token: tok }
  if (ticket) s.ticket = ticket
  shares(state).unshift(s)
  return s
}

const subtitle = (x: Share) =>
  [x.kind === 'sealed' ? `sealed to ${x.recipient}` : x.kind, `expires in ${plural(x.expires_in_days, 'day', 'days')}`, plural(x.views, 'view', 'views'), x.view_limit ? `limit ${x.view_limit}` : null, x.last_viewer ? `last: ${x.last_viewer}` : null]
    .filter(Boolean)
    .join(' · ')

const shareItem = (x: Share) => ({
  id: x.id,
  title: x.title,
  subtitle: subtitle(x),
  badge: x.ticket ?? 'workspace',
  actions: [
    // A sealed share opens only for its recipient: there is no link to copy. A show-once link cannot be copied again:
    // the row says so; pressing it explains (copy_link answers 409).
    ...(x.kind === 'sealed' ? [] : [{ label: x.kind === 'show-once' ? 'Shown once' : 'Copy link', action: 'copy_link', args: { id: x.id }, variant: 'ghost' as const }]),
    { label: 'Extend 7 days', action: 'extend', args: { id: x.id }, variant: 'ghost' as const },
    { label: 'Revoke', action: 'revoke', args: { id: x.id }, variant: 'danger' as const },
  ],
})

const seedState = () => ({
  apps: withStats(seedApps()),
  shares: seedShares(),
  settings: { default_expiry_days: 7, namespace: 'acme', allow_artifacts: false },
  decided: [],
})

/** Busy day: 12 apps and 40 shares in all (workspaces other than DEMO get a third), some of them on restricted tickets. */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = seedState()
  const demo = store.workspaces.find((w) => w.id === ws)?.prefix === 'DEMO'
  const apps: App[] = [
    ['Meter quality board', 'streamlit'], ['Tariff explorer', 'streamlit'], ['Invoice preview', 'static'], ['Outage map', 'static'], ['Usage forecast', 'notebook'],
    ['Reconciliation viewer', 'streamlit'], ['Customer segments', 'notebook'], ['Grid fee calculator', 'static'], ['CO2 report', 'notebook'],
  ].map(([name, kind], i) => {
    const status = (['running', 'running', 'stopped', 'running', 'failed', 'running', 'stopped', 'running', 'failed'] as const)[i]
    return {
      id: `app_b${i + 1}`,
      name,
      kind: kind as App['kind'],
      folder: `apps/${name.toLowerCase().replace(/ /g, '-')}`,
      status,
      recipients: status === 'running' ? rng.int(1, 5) : 0,
      log: status === 'failed' ? ['Installing requirements.txt', `ERROR: ${name} needs pandas>=2.2`, 'Build failed (exit 1)'] : Array.from({ length: rng.int(6, 24) }, (_, n) => `${String(n).padStart(2, '0')}:${String(rng.int(0, 59)).padStart(2, '0')} served ${rng.int(1, 40)} requests`),
    }
  })
  const tickets = briefs(store, ws)
  const restricted = tickets.filter((t) => t.restricted)
  const others = rng.shuffle(tickets.filter((t) => !t.restricted && ['done', 'testing', 'in-progress'].includes(t.status)))
  const bound = [...restricted.slice(0, 3), ...others]
  const kinds: ShareKind[] = ['secret link', 'secret link', 'public link', 'sealed', 'show-once', 'secret link']
  const people = ['Mara', 'Severin', 'Tom']
  const titles = ['Before/after report', 'Reconciliation table', 'Run summary', 'Sample rows', 'Tariff notes', 'Invoice preview', 'Findings', 'Meter list']
  const shares: Share[] = Array.from({ length: 35 }, (_, i) => {
    const kind = kinds[i % kinds.length]
    const t = i < 30 ? bound[i % bound.length] : undefined
    return {
      id: `sh_b${i + 1}`,
      ...(t ? { ticket: t.key } : {}),
      title: `${titles[i % titles.length]}${i >= 8 ? ` ${Math.floor(i / 8) + 1}` : ''}`,
      kind,
      ...(kind === 'sealed' ? { recipient: people[i % people.length] } : {}),
      expires_in_days: rng.int(1, 30),
      views: rng.int(0, 240),
      last_viewer: rng.chance(0.8) ? `${rng.pick([...people, 'anonymous'])}, ${rng.int(1, 9)} ${rng.pick(['min', 'h', 'days'])} ago` : null,
      token: kind === 'sealed' || kind === 'show-once' ? null : tokenOf(i, 11),
    }
  })
  state.apps.push(...apps.slice(0, demo ? 9 : 2).map((x, i) => ({ ...x, stats: appStats(x, 100 + i) })))
  state.shares.push(...(demo ? shares : shares.slice(0, 10)))
  return state
}

/** A rebuild: running from now, a new (simulated) commit that went through. */
function redeployed(x: App, now: string) {
  const st = statsOf(x)
  st.since = now
  st.deploy = { commit: appStats({ id: `${x.id}.${now}`, status: 'running' }, -1).deploy.commit, at: now, ok: true }
}

registerAddon({
  name: 'publish',
  seed: seedState,
  seedBusy,

  view(state, c) {
    const a = apps(state)
    // A share tied to a ticket is shown only to people who can see that ticket.
    const sh = shares(state).filter((x) => !x.ticket || canSeeTicket(c, x.ticket))
    const running = a.filter((x) => x.status === 'running').length
    const failed = a.filter((x) => x.status === 'failed')
    const byTicket: Record<string, ReturnType<typeof shareItem>[]> = {}
    for (const x of sh) if (x.ticket) (byTicket[x.ticket] ??= []).push(shareItem(x))
    return {
      // Overrides the raw list. Secret tokens never ride in the state (anyone with the page would hold every link):
      // a member gets a link from copy_link or share_once, in the action's answer.
      shares: sh.map(({ token: _token, ...x }) => x),
      summary: `${plural(running, 'app', 'apps')} running · ${plural(failed.length, 'failed build', 'failed builds')}`,
      // Today's glance line under "n of m apps running": a failed build first, then the shares.
      todayHint: [failed.length ? plural(failed.length, 'failed build', 'failed builds') : null, plural(sh.length, 'live share', 'live shares'), plural(sh.reduce((n, x) => n + x.views, 0), 'view', 'views')].filter(Boolean).join(' · '),
      liveShares: sh.length,
      views: sh.reduce((n, x) => n + x.views, 0),
      appCount: a.length,
      runningCount: running,
      // canStart/canStop drive the rows' `when`: Start only while stopped, Stop only while running.
      appRows: a.map((x) => ({ id: x.id, name: x.name, kind: x.kind, folder: x.folder, status: x.status, recipients: x.recipients, canStart: x.status === 'stopped', canStop: x.status === 'running', note: x.status === 'failed' ? failedLine(x) : '' })),
      // The Shares tab: one compact row per share (kind and expiry as plain cells; `when` picks Copy link or Shown once).
      shareRows: sh.map((x) => ({
        id: x.id,
        title: x.title,
        kind: x.kind === 'sealed' ? `sealed to ${x.recipient}` : x.kind,
        ticket: x.ticket ?? 'workspace',
        expires: `${plural(x.expires_in_days, 'day', 'days')}`,
        views: x.views,
        last: x.last_viewer ?? '–',
        canCopy: x.kind !== 'show-once',
        shownOnce: x.kind === 'show-once',
      })),
      // The Apps rows' details (table rowDetail, keyed by the row's id).
      appDetails: Object.fromEntries(a.map((x) => [x.id, appDetail(x, settingsOf(state), c.store.now())])),
      // Failed builds, once, above the tabs (it needs a person). Empty while nothing failed. The full log is behind "Show log".
      attentionNode: failed.length
        ? {
            type: 'stack',
            children: [
              {
                type: 'list',
                items: failed.map((x) => ({
                  id: x.id,
                  title: `${x.name} failed to build`,
                  subtitle: 'Redeploy rebuilds it from its folder. Show log has the details.',
                  status: 'error' as const,
                  actions: [{ label: 'Redeploy', action: 'redeploy', args: { id: x.id }, variant: 'secondary' as const, primary: true }],
                })),
              },
              // The whole build log, closed: it is what "Show log" opens (the `logs` action only toasts the last lines).
              ...failed.map((x) => ({ type: 'fold', label: failed.length > 1 ? `Show log · ${x.name}` : 'Show log', node: { type: 'code', language: 'text', text: x.log.join('\n') } })),
            ],
          }
        : { type: 'stack', children: [] },
      // The ticket panel binds `addon.sharesByTicket.$ticket`; generated from state here and nowhere else.
      sharesByTicket: byTicket,
    }
  },

  // The failed-build decision exists only while an app is failed; the report decision until it is decided.
  decisions(state, pkg): AddonDecision[] {
    const done = (state.decided as string[] | undefined) ?? []
    const failed = apps(state).some((x) => x.status === 'failed')
    return pkg.filter((d) => !done.includes(d.id) && (d.id !== 'dec_publish_failed_build' || failed))
  },

  actions: {
    share(ctx) {
      const { store, ticket, state } = ctx
      if (!ticket || !canSeeTicket(ctx, ticket)) return invalid('Pick a ticket first.')
      const days = settingsOf(state).default_expiry_days
      const x = newShare(state, ticket, 'secret link', `share/${ticket.toLowerCase()}`, token(state))
      store.append(ticket, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
      return {
        ok: true,
        message: `Shared ${ticket} as a secret link for ${plural(days, 'day', 'days')}.`,
        changed: true,
        secret: { label: `Secret link for ${ticket}`, value: linkOf(settingsOf(state), x.token!), note: `Anyone with the link can read it for ${plural(days, 'day', 'days')}. You can copy it again from the Shares list.` },
      }
    },
    share_once(ctx) {
      const { store, ticket, state, body } = ctx
      // A link is always to one ticket (or its report): outside a ticket there is nothing to share.
      if (!ticket || !canSeeTicket(ctx, ticket)) return invalid('Pick a ticket first.')
      // Core's small dialog asks first (manifest `confirm: 'options'`); the host checks what came back.
      const what = body.what === undefined ? 'ticket' : String(body.what)
      const days = body.expires_days === undefined ? settingsOf(state).default_expiry_days : Number(body.expires_days)
      const limit = body.view_limit === undefined ? 1 : Number(body.view_limit)
      if (!(what in SHARE_WHAT)) return invalid('Choose what to share: the ticket or its report.')
      if (!SHARE_DAYS.includes(days)) return invalid(`Choose how long the link works: ${SHARE_DAYS.join(', ')} days.`)
      if (!SHARE_VIEWS.includes(limit)) return invalid('Choose how many times it can be opened.')
      const tok = token(state)
      const sh = newShare(state, ticket, 'show-once', `${ticket} ${SHARE_WHAT[what]} one-time link`, null)
      sh.expires_in_days = days
      if (limit > 0) sh.view_limit = limit
      store.append(ticket, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
      const rule = `${limit > 0 ? `opens ${limit === 1 ? 'once' : `${limit} times`}` : 'no view limit'}, works for ${plural(days, 'day', 'days')}`
      return {
        ok: true,
        message: `Created a one-time link (${rule}).`,
        changed: true,
        secret: { label: `One-time link for ${ticket}`, value: linkOf(settingsOf(state), tok), note: `${SHARE_WHAT[what][0].toUpperCase()}${SHARE_WHAT[what].slice(1)}: ${rule}. It is shown once and cannot be copied again. Revoke it and make a new one if you lose it.` },
      }
    },
    copy_link(ctx) {
      const { state, body } = ctx
      const x = visibleShare(ctx, body.id)
      if (!x) return notFound('That share no longer exists.')
      if (x.kind === 'show-once') return conflict('publish.shown_once', 'This link was shown once and cannot be copied again.', 'Revoke it and make a new one.')
      if (!x.token) return conflict('publish.sealed', `Sealed to ${x.recipient}: it opens only for them, so there is no link to copy.`)
      return { ok: true, message: `Link for ${x.title} ready.`, secret: { label: x.title, value: linkOf(settingsOf(state), x.token), note: `Expires in ${plural(x.expires_in_days, 'day', 'days')}.` } }
    },
    extend(ctx) {
      const { body } = ctx
      const x = visibleShare(ctx, body.id)
      if (!x) return notFound('That share no longer exists.')
      x.expires_in_days += 7
      return { ok: true, message: `${x.title} now expires in ${plural(x.expires_in_days, 'day', 'days')}.`, changed: true }
    },
    revoke(ctx) {
      const { store, state, body } = ctx
      const list = shares(state)
      const i = list.findIndex((s) => s.id === body.id && (!s.ticket || canSeeTicket(ctx, s.ticket)))
      if (i < 0) return notFound('That share no longer exists.')
      const [x] = list.splice(i, 1)
      if (x.ticket && store.hasTicket(x.ticket)) store.append(x.ticket, { type: 'publish.revoked', actor: { kind: 'addon', id: 'publish' } })
      return { ok: true, message: `Revoked ${x.title}. The link stops working now.`, changed: true }
    },
    start({ state, body, store }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return notFound('No such app.')
      if (x.status === 'failed') return conflict('publish.build_failed', `${x.name} failed to build.`, 'Redeploy it first.')
      x.status = 'running'
      statsOf(x).since = store.now()
      x.log.push('Started')
      return { ok: true, message: `${x.name} is running.`, changed: true }
    },
    stop({ state, body, store }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return notFound('No such app.')
      x.status = 'stopped'
      statsOf(x).since = store.now()
      x.log.push('Stopped')
      // Acts at once; the toast carries Undo, which starts it again.
      return { ok: true, message: `${x.name} stopped.`, changed: true, undo: { action: 'start', args: { id: x.id } } }
    },
    logs({ state, body }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return notFound('No such app.')
      return { ok: true, message: `${x.name}: ${x.log.slice(-3).join(' / ')}` }
    },
    redeploy({ state, body, store }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return notFound('No such app.')
      x.status = 'running'
      redeployed(x, store.now())
      x.log.push('Redeployed', 'Started')
      return { ok: true, message: `${x.name} rebuilt and running.`, changed: true }
    },
    // A decision action: core checked who decides, that it is open and that the option is one of its options.
    decide(ctx) {
      const { store, state, body } = ctx
      const open = ctx.decision
      if (!open) return conflict('decision.closed', 'That decision is closed.') // only when the manifest lacks `decision: true`
      const id = open.id
      const option = String(body.option)
      markDecided(state, id)
      if (id === 'dec_publish_failed_build') {
        const ops = apps(state).find((a) => a.id === 'app_ops')
        if (option === 'retry' && ops) {
          ops.status = 'running'
          redeployed(ops, store.now())
          ops.log.push('Rebuilt from the last good version', 'Started')
        }
        return { ok: true, message: option === 'retry' ? 'Ops notebook rebuilt from the last good version.' : 'Left as it is.', changed: true }
      }
      if (open.ticket && store.hasTicket(open.ticket)) {
        store.append(open.ticket, { type: 'publish.decided', actor: { kind: 'addon', id: 'publish' }, option })
        if (option === 'yes') newShare(state, open.ticket, 'secret link', 'Before/after report', token(state))
      }
      return { ok: true, message: option === 'yes' ? 'Published as a secret link for 7 days.' : 'Not published.', changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
