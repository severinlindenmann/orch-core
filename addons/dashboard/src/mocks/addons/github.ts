import { registerAddon } from './registry'

// github: pull requests (code reviews) and the external issues lane. Addon state is the only store for PRs and issues;
// the ticket panel reads `addon.prByTicket.$ticket` (see view()), nothing is written to ticket addon data.

type CheckStatus = 'pass' | 'fail' | 'pending'
type Review = 'none' | 'requested' | 'approved' | 'changes requested'
interface Check {
  name: string
  status: CheckStatus
}
interface Pr {
  /** `<repo>#<number>`, unique. */
  id: string
  repo: string
  number: number
  title: string
  ticket: string
  branch: string
  state: 'open' | 'draft' | 'merged'
  checks: Check[]
  review: Review
  author: { kind: 'agent' | 'person'; name: string }
  additions: number
  deletions: number
  files: number
  updated_at: string
}
interface Issue {
  id: string
  repo: string
  number: number
  title: string
  label: 'bug' | 'docs' | 'chore' | 'feature'
}

const DBT = 'acme-energy/energy-dbt'
const API = 'acme-energy/billing-api'
const at = (iso: string) => iso
const pr = (p: Omit<Pr, 'id'>): Pr => ({ id: `${p.repo}#${p.number}`, ...p })
const agent = (name: string) => ({ kind: 'agent' as const, name })
const person = (name: string) => ({ kind: 'person' as const, name })
const checks = (...c: [string, CheckStatus][]): Check[] => c.map(([name, status]) => ({ name, status }))

const seedPrs = (): Pr[] => [
  pr({ repo: DBT, number: 31, title: 'Load tariff tables as dbt seeds', ticket: 'DEMO-0043', branch: 'feat/DEMO-0043-tariff-seeds', state: 'draft', checks: checks(['lint', 'pass'], ['dbt build', 'pending'], ['schema tests', 'pending']), review: 'requested', author: agent('claude-code'), additions: 214, deletions: 12, files: 9, updated_at: at('2026-10-09T11:12:00Z') }),
  pr({ repo: DBT, number: 29, title: 'Add billing reconciliation tests', ticket: 'DEMO-0041', branch: 'feat/DEMO-0041-reconciliation', state: 'open', checks: checks(['lint', 'pass'], ['dbt build', 'pass'], ['schema tests', 'pass']), review: 'requested', author: agent('claude-code'), additions: 186, deletions: 4, files: 6, updated_at: at('2026-10-09T09:30:00Z') }),
  pr({ repo: DBT, number: 27, title: 'Normalize meter reading timestamps to UTC', ticket: 'DEMO-0042', branch: 'feat/DEMO-0042-utc', state: 'merged', checks: checks(['lint', 'pass'], ['dbt build', 'pass']), review: 'approved', author: person('Mara'), additions: 98, deletions: 61, files: 7, updated_at: at('2026-10-07T15:00:00Z') }),
  pr({ repo: DBT, number: 33, title: 'Add freshness checks to sources', ticket: 'DEMO-0037', branch: 'feat/DEMO-0037-freshness', state: 'open', checks: checks(['lint', 'pass'], ['dbt build', 'fail'], ['source freshness', 'fail']), review: 'changes requested', author: agent('claude-code'), additions: 73, deletions: 9, files: 4, updated_at: at('2026-10-09T07:45:00Z') }),
  pr({ repo: API, number: 58, title: 'Fix duplicate meter ids in dim_meter', ticket: 'DEMO-0046', branch: 'fix/DEMO-0046-dup-meters', state: 'open', checks: checks(['unit tests', 'pass'], ['contract tests', 'pass']), review: 'none', author: person('Mara'), additions: 41, deletions: 17, files: 3, updated_at: at('2026-10-08T16:20:00Z') }),
  pr({ repo: API, number: 61, title: 'Rotate warehouse service credentials', ticket: 'DEMO-0044', branch: 'chore/DEMO-0044-rotate-creds', state: 'open', checks: checks(['unit tests', 'pass'], ['contract tests', 'pending']), review: 'requested', author: agent('claude-code'), additions: 22, deletions: 22, files: 2, updated_at: at('2026-10-09T10:55:00Z') }),
]

const issue = (repo: string, number: number, title: string, label: Issue['label']): Issue => ({ id: `${repo}#${number}`, repo, number, title, label })
const seedIssues = (): Issue[] => [
  issue(DBT, 118, 'Seed loader fails on BOM files', 'bug'),
  issue(DBT, 121, 'Document tariff naming', 'docs'),
  issue(DBT, 124, 'Bump dbt-utils', 'chore'),
  issue(DBT, 127, 'Add a staging model for gas meters', 'feature'),
  issue(API, 64, 'Invoice total rounds half down', 'bug'),
  issue(API, 66, 'Pin the OpenAPI generator version', 'chore'),
  issue(API, 69, 'Explain the retry policy in the README', 'docs'),
  issue(API, 71, 'Expose tariff valid-from in the API', 'feature'),
]

const prs = (state: Record<string, unknown>) => state.prs as Pr[]
const issues = (state: Record<string, unknown>) => state.issues as Issue[]
const prUrl = (p: Pr) => `https://github.com/${p.repo}/pull/${p.number}`

/** One word for the checks of a PR: any failure beats pending beats pass. */
const summary = (p: Pr): CheckStatus => (p.checks.some((c) => c.status === 'fail') ? 'fail' : p.checks.some((c) => c.status === 'pending') ? 'pending' : 'pass')
const pending = (p: Pr) => p.checks.some((c) => c.status === 'pending')

function ago(fromIso: string, nowIso: string): string {
  const min = Math.max(0, Math.round((Date.parse(nowIso) - Date.parse(fromIso)) / 60000))
  if (min < 60) return `${min} min ago`
  const h = Math.round(min / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  return `${d} ${d === 1 ? 'day' : 'days'} ago`
}

registerAddon({
  name: 'github',
  seed: () => ({
    prs: seedPrs(),
    issues: seedIssues(),
    settings: { org: 'acme-energy', link_prs: true, repos: `${DBT}, ${API}`, poll_minutes: 5 },
  }),

  view(state, { store }) {
    const list = prs(state)
    const now = store.now()
    const open = list.filter((p) => p.state !== 'merged')
    const byTicket: Record<string, unknown> = {}
    for (const p of list) {
      byTicket[p.ticket] = {
        repo: p.repo,
        number: p.number,
        branch: p.branch,
        state: p.state,
        checks: summary(p),
        review: p.review,
        author: `${p.author.name} (${p.author.kind})`,
        diff: `+${p.additions} −${p.deletions} in ${p.files} files`,
        url: prUrl(p),
        checkPairs: p.checks.map((c) => ({ label: c.name, value: c.status })),
      }
    }
    return {
      openPrs: open.length,
      needReview: open.filter((p) => p.review === 'requested').length,
      checksFailing: open.filter((p) => summary(p) === 'fail').length,
      prRows: list.map((p) => ({ id: p.id, repo: p.repo, pr: `#${p.number}`, title: p.title, ticket: p.ticket, checks: summary(p), review: p.review, updated: ago(p.updated_at, now) })),
      prByTicket: byTicket,
      todayItems: open
        .filter((p) => p.review === 'requested')
        .map((p) => ({ title: `#${p.number} ${p.title}`, subtitle: `${p.repo} · ${p.ticket}`, badge: `checks ${summary(p)}` })),
      issueItems: issues(state).map((i) => ({
        title: i.title,
        subtitle: i.id,
        badge: i.label,
        actions: [{ label: 'Import as ticket', action: 'import', args: { id: i.id }, variant: 'secondary' as const }],
      })),
    }
  },

  actions: {
    import({ store, ws, state, body }) {
      const list = issues(state)
      const i = list.findIndex((x) => x.id === body.id)
      if (i < 0) return { ok: true, message: 'Nothing to import.' }
      const x = list[i]
      const res = store.importGithubIssue(ws, { title: x.title, subtitle: x.id, badge: x.label })
      if (res.changed) list.splice(i, 1)
      return res
    },
    refresh({ store, state, body, ticket }) {
      const list = prs(state)
      const target = list.find((p) => (body.id ? p.id === body.id : ticket ? p.ticket === ticket : pending(p)))
      if (!target) return { ok: true, message: ticket ? `${ticket} has no pull request.` : 'No pending checks.' }
      if (!pending(target)) return { ok: true, message: `No pending checks on #${target.number}.` }
      for (const c of target.checks) if (c.status === 'pending') c.status = 'pass'
      target.updated_at = store.now()
      return { ok: true, message: `Checked GitHub: #${target.number} ${target.title} checks passed.`, changed: true }
    },
    approve({ store, state, body }) {
      const target = prs(state).find((p) => p.id === body.id)
      if (!target) return { ok: true, message: 'That pull request no longer exists.' }
      if (target.state === 'merged') return { ok: true, message: `#${target.number} is already merged.` }
      if (target.review === 'approved') return { ok: true, message: `#${target.number} is already approved.` }
      target.review = 'approved'
      target.updated_at = store.now()
      return { ok: true, message: `Approved #${target.number} on GitHub (mock).`, changed: true }
    },
    open({ state, body }) {
      const target = prs(state).find((p) => p.id === body.id)
      if (!target) return { ok: true, message: 'That pull request no longer exists.' }
      return { ok: true, message: `Opening #${target.number} on GitHub.`, url: prUrl(target) }
    },
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
