import type { AddonDecision } from '@/api/types'
import { getAddon, markDecided, openDecisions, registerAddon } from './registry'

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
  /** Token of the link; null for a show-once link (only ever shown in the toast) and for a sealed share. */
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
  [x.kind === 'sealed' ? `sealed to ${x.recipient}` : x.kind, `expires in ${plural(x.expires_in_days, 'day', 'days')}`, plural(x.views, 'view', 'views'), x.last_viewer ? `last: ${x.last_viewer}` : null]
    .filter(Boolean)
    .join(' · ')

const shareItem = (x: Share) => ({
  title: x.title,
  subtitle: subtitle(x),
  badge: x.ticket ?? 'workspace',
  actions: [
    { label: 'Copy link', action: 'copy_link', args: { id: x.id }, variant: 'ghost' as const },
    { label: 'Extend 7 days', action: 'extend', args: { id: x.id }, variant: 'ghost' as const },
    { label: 'Revoke', action: 'revoke', args: { id: x.id }, variant: 'danger' as const },
  ],
})

registerAddon({
  name: 'publish',
  seed: () => ({
    apps: seedApps(),
    shares: seedShares(),
    settings: { default_expiry_days: 7, namespace: 'acme', allow_artifacts: false },
    decided: [],
  }),

  view(state) {
    const a = apps(state)
    const sh = shares(state)
    const running = a.filter((x) => x.status === 'running').length
    const failed = a.filter((x) => x.status === 'failed')
    const byTicket: Record<string, ReturnType<typeof shareItem>[]> = {}
    for (const x of sh) if (x.ticket) (byTicket[x.ticket] ??= []).push(shareItem(x))
    return {
      summary: `${plural(running, 'app', 'apps')} running · ${plural(failed.length, 'failed build', 'failed builds')}`,
      liveShares: sh.length,
      views: sh.reduce((n, x) => n + x.views, 0),
      appCount: a.length,
      runningCount: running,
      appRows: a.map((x) => ({ id: x.id, name: x.name, kind: x.kind, folder: x.folder, status: x.status, recipients: x.recipients })),
      shareItems: sh.map(shareItem),
      // The ticket panel binds `addon.sharesByTicket.$ticket`; generated from state here and nowhere else.
      sharesByTicket: byTicket,
      attention: failed.map((x) => ({
        title: x.name,
        subtitle: x.log[x.log.length - 2] ?? 'Build failed',
        status: 'error' as const,
        actions: [
          { label: 'Logs', action: 'logs', args: { id: x.id }, variant: 'ghost' as const },
          { label: 'Redeploy', action: 'redeploy', args: { id: x.id }, variant: 'secondary' as const },
        ],
      })),
      failedLog: failed.length ? failed.map((x) => `${x.name}\n${x.log.join('\n')}`).join('\n\n') : 'No failed builds.',
    }
  },

  // The failed-build decision exists only while an app is failed; the report decision until it is decided.
  decisions(state, pkg): AddonDecision[] {
    const done = (state.decided as string[] | undefined) ?? []
    const failed = apps(state).some((x) => x.status === 'failed')
    return pkg.filter((d) => !done.includes(d.id) && (d.id !== 'dec_publish_failed_build' || failed))
  },

  actions: {
    share({ store, ticket, state }) {
      if (!ticket || !store.hasTicket(ticket)) return { ok: true, message: 'Pick a ticket first.' }
      const days = settingsOf(state).default_expiry_days
      newShare(state, ticket, 'secret link', `share/${ticket.toLowerCase()}`, token(state))
      store.append(ticket, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
      return { ok: true, message: `Shared ${ticket} as a secret link for ${plural(days, 'day', 'days')}.`, changed: true }
    },
    share_once({ store, ticket, state }) {
      const tok = token(state)
      const label = ticket && store.hasTicket(ticket) ? ticket : undefined
      newShare(state, label, 'show-once', label ? `${label} one-time link` : 'One-time link', null)
      if (label) store.append(label, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
      return { ok: true, message: `Link copied, shown once: ${linkOf(settingsOf(state), tok)}`, changed: true }
    },
    copy_link({ state, body }) {
      const x = shares(state).find((s) => s.id === body.id)
      if (!x) return { ok: true, message: 'That share no longer exists.' }
      if (x.kind === 'show-once') return { ok: true, message: 'This link was shown once and cannot be copied again. Revoke it and make a new one.' }
      if (!x.token) return { ok: true, message: `Sealed to ${x.recipient}: it opens only for them, so there is no link to copy.` }
      return { ok: true, message: `Link copied: ${linkOf(settingsOf(state), x.token)}` }
    },
    extend({ state, body }) {
      const x = shares(state).find((s) => s.id === body.id)
      if (!x) return { ok: true, message: 'That share no longer exists.' }
      x.expires_in_days += 7
      return { ok: true, message: `${x.title} now expires in ${x.expires_in_days} days.`, changed: true }
    },
    revoke({ store, state, body }) {
      const list = shares(state)
      const i = list.findIndex((s) => s.id === body.id)
      if (i < 0) return { ok: true, message: 'That share no longer exists.' }
      const [x] = list.splice(i, 1)
      if (x.ticket && store.hasTicket(x.ticket)) store.append(x.ticket, { type: 'publish.revoked', actor: { kind: 'addon', id: 'publish' } })
      return { ok: true, message: `Revoked ${x.title}. The link stops working now.`, changed: true }
    },
    start({ state, body }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return { ok: true, message: 'No such app.' }
      if (x.status === 'failed') return { ok: true, message: `${x.name} failed to build. Redeploy it first.` }
      x.status = 'running'
      x.log.push('Started')
      return { ok: true, message: `${x.name} is running.`, changed: true }
    },
    stop({ state, body }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return { ok: true, message: 'No such app.' }
      x.status = 'stopped'
      x.log.push('Stopped')
      return { ok: true, message: `${x.name} stopped.`, changed: true }
    },
    logs({ state, body }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return { ok: true, message: 'No such app.' }
      return { ok: true, message: `${x.name}: ${x.log.slice(-3).join(' / ')}` }
    },
    redeploy({ state, body }) {
      const x = apps(state).find((a) => a.id === body.id)
      if (!x) return { ok: true, message: 'No such app.' }
      x.status = 'running'
      x.log.push('Redeployed', 'Started')
      return { ok: true, message: `${x.name} rebuilt and running.`, changed: true }
    },
    decide({ store, state, body }) {
      const id = String(body.id ?? '')
      const option = String(body.option ?? '')
      // The store already refuses a closed decision; look the open one up the same way (runtime list, not the package's).
      const open = openDecisions(getAddon('publish'), state, store.addons.find((a) => a.name === 'publish')?.decisions ?? []).find((d) => d.id === id)
      if (!open) return { ok: true, message: 'That decision is closed.' }
      markDecided(state, id)
      if (id === 'dec_publish_failed_build') {
        const ops = apps(state).find((a) => a.id === 'app_ops')
        if (option === 'retry' && ops) {
          ops.status = 'running'
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
