// Mandates, PREVIEW ONLY (docs/concept-mandates.md; owner decision 10 Oct 2026 evening: the wide mandate). A mock of
// how it would look, for the owner to see. Nothing here is real:
//  - nothing is signed, no Touch ID runs, and no request reaches a signing or decision path (tickets' actions, grants,
//    addon ops, settings, relay): this module keeps its own little state and never touches the workspace or ticket logs;
//  - the preflight is honest: none of the four prerequisites exists in this build, so a real host would refuse every
//    mandate. "Show the mandate anyway" only turns the preview on;
//  - the decision log is seeded ("as if the mandate had run for three days"): a mix of gate approvals, a code review,
//    an unblocked ticket, a factory permit, a factory enabled, a full run started and a grant.
// Off by default. Kept in the browser under its own key (the demo's Reset clears it); tests keep it in memory.
import { MAX_MANDATE_DAYS, type MandateDecision, type MandateDecisionKind, type MandatePreflightCheck, type MandateRefusal, type MandatesPreviewRequest, type MandatesPreviewState, type PreviewMandate } from '@/api/mandatesPreview'
import { can } from '@/api/permissions'
import type { MockStore, StoreFailure } from './store'

export const STORAGE_KEY = 'orch.preview.mandates'
/** How long the host takes to acknowledge a Stop (mock clock). */
export const STOP_ACK_MS = import.meta.env.MODE === 'test' ? 150 : 1_500
const DAY = 86_400_000
const DEFAULT_DAYS = 7
/** Where the seeded log starts in the workspace log, and where a Stop draws its boundary. */
const FIRST_SEQ = 1831
const BOUNDARY_SEQ = 1842

export const PREFLIGHT: MandatePreflightCheck[] = [
  {
    id: 'p1',
    title: 'P2 custody',
    detail: 'The host holds every signing key, agents run under their own OS user, addons are isolated from host code and state, the keychain backend is verified.',
    state: 'missing',
    note: 'Not available in this build.',
  },
  {
    id: 'p2',
    title: 'Isolated execution for mandated sessions',
    detail: 'No deploy or production credentials, an egress allowlist, a sandboxed test runner, no CI or deploy with secrets from develop while a mandate is in force.',
    state: 'missing',
    note: 'Not available in this build.',
  },
  {
    id: 'p3',
    title: 'Host-minted session identities and a host-provisioned checker',
    detail: 'The host mints the orchestrator’s and the checker’s identities; the orchestrator cannot pick, prompt or feed the checker.',
    state: 'missing',
    note: 'Not available in this build.',
  },
  {
    id: 'p4',
    title: 'Typed effects, durable counters, time and rollback guard',
    detail: 'Core validates every delegable action against a typed effect; counters survive restarts; a clock jump or restored state suspends every mandate.',
    state: 'missing',
    note: 'Not available in this build.',
  },
]

/** What the mandate may do in your name (owner decision 10 Oct evening). Core text, shown in full. */
export const MAY: string[] = [
  'Approve agents that wait for an approval: requirements, plan, verdicts and the code review gate.',
  'Answer agents\u2019 questions (typed choices only).',
  'Unblock tickets.',
  'Answer addon decisions and factory permits.',
  'Enable factories and start factory runs, including Deliver (after the hold window).',
  'Issue grants that start agents (they stay inside this mandate).',
]

/** What stays human, always (owner decision 10 Oct evening; concept §Summary). Core text, shown in full. */
export const ALWAYS_HUMAN: string[] = [
  'Settings and policies.',
  'Addons: install, update, capabilities.',
  'Members and roles.',
  'Devices.',
  'Relay pairing.',
  'Secrets and connections.',
  'Changes to protected paths (listed below).',
  'Another mandate: a mandate never issues, extends or renews one.',
  'Overriding a person\u2019s \u201cno\u201d: it stays until a person lifts it.',
]

/** Concept §2.2: the workspace default. A person can extend it, never shrink it below this. */
export const PROTECTED_PATHS: string[] = [
  'CI workflows (.github/workflows/**)',
  'Build and test tooling (scripts/**, Makefile, vitest/jest config)',
  'Deploy config (deploy/**, Dockerfile, *.tf)',
  'Dependency manifests and lockfiles (package.json, package-lock.json, pyproject.toml, uv.lock)',
  'orch config (.orch/**)',
  'AGENTS*.md, skills and hooks',
  'Code classed security',
]

interface Sim {
  on: boolean
  mandate: PreviewMandate | null
  /** The next mandate number (md_3 is the first: the factory charter and one earlier mandate came before, as in the concept). */
  next: number
}
interface Saved {
  v: 1
  ws: Record<string, Sim>
}

const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
const fail = (status: number, code: string, message: string, hint?: string): StoreFailure => ({ ok: false, status, code, message, ...(hint ? { hint } : {}) })

/** The preview's state per workspace, beside the store (never in a workspace or ticket log). */
export class MandatesPreviewHost {
  private sims = new Map<string, Sim>()
  private loaded = false
  constructor(private store: MockStore) {}

  private storage(): Storage | null {
    if (!this.store.persisting) return null
    try {
      return globalThis.localStorage ?? null
    } catch {
      return null
    }
  }

  private load() {
    if (this.loaded) return
    this.loaded = true
    try {
      const raw = this.storage()?.getItem(STORAGE_KEY)
      const saved = raw ? (JSON.parse(raw) as Partial<Saved>) : null
      if (saved?.v === 1 && saved.ws && typeof saved.ws === 'object') {
        for (const [ws, sim] of Object.entries(saved.ws)) {
          if (!sim || typeof sim.on !== 'boolean') continue
          const m = sim.mandate ?? null
          // The demo clock restarts on a reload: a Stop sent before it is acknowledged now (never stuck on "Stopping…").
          if (m?.state === 'stopping' && m.stop) {
            m.state = 'stopped'
            m.stop.boundary_seq = BOUNDARY_SEQ
          }
          this.sims.set(ws, { on: sim.on, mandate: m, next: sim.next ?? 3 })
        }
      }
    } catch {
      /* unreadable: the preview starts off */
    }
  }

  private save() {
    try {
      const s = this.storage()
      if (!s) return
      if (this.sims.size === 0) s.removeItem(STORAGE_KEY)
      else s.setItem(STORAGE_KEY, JSON.stringify({ v: 1, ws: Object.fromEntries(this.sims) } satisfies Saved))
    } catch {
      /* storage blocked: the preview lasts until a reload */
    }
  }

  /** The demo's Reset: off everywhere. */
  reset() {
    this.sims.clear()
    this.loaded = true
    try {
      this.storage()?.removeItem(STORAGE_KEY)
    } catch {
      /* ignore */
    }
  }

  private sim(ws: string): Sim {
    this.load()
    let s = this.sims.get(ws)
    if (!s) {
      s = { on: false, mandate: null, next: 3 }
      this.sims.set(ws, s)
    }
    return s
  }

  private name(ws: string, person: string) {
    return this.store.workspaces.find((w) => w.id === ws)?.members.find((m) => m.person === person)?.name ?? person
  }

  /** Tickets the seeded log decides on: the factory epic's children first, then other open work (never restricted). */
  private tickets(ws: string) {
    const all = this.store.listTickets(ws).filter((t) => !t.restricted && t.type !== 'epic')
    const kids = all.filter((t) => t.parent?.endsWith('-0050')).sort((a, b) => a.key.localeCompare(b.key))
    const rest = all.filter((t) => !t.parent).sort((a, b) => a.key.localeCompare(b.key))
    return [...kids, ...rest]
  }

  private orchestrators(ws: string) {
    return this.store
      .agents(ws)
      .filter((a) => !a.parent && a.state !== 'stopped')
      .map((a) => ({ name: a.name, session: a.session }))
  }

  state(ws: string): MandatesPreviewState {
    const s = this.sim(ws)
    // The host acknowledges a Stop a moment later (mock clock): "Stopping…" becomes "Stopped at #1842".
    const m = s.mandate
    if (m?.state === 'stopping' && m.stop && Date.parse(this.store.now()) - Date.parse(m.stop.requested_at) >= STOP_ACK_MS) {
      m.state = 'stopped'
      m.stop.boundary_seq = BOUNDARY_SEQ
      this.save()
    }
    return {
      preview: true,
      on: s.on,
      preflight: PREFLIGHT,
      orchestrators: this.orchestrators(ws),
      may: MAY,
      always_human: ALWAYS_HUMAN,
      protected_paths: PROTECTED_PATHS,
      // A copy, as a host would serialise it (the cache must never hold the mock's own object).
      mandate: s.on && m ? structuredClone(m) : null,
    }
  }

  /** A mandate as if it had run for three days across the workspace (the seeded log). */
  private seedMandate(ws: string, s: Sim, orchestrator: { name: string; session: string }, days: number): PreviewMandate | StoreFailure {
    if (!Number.isInteger(days) || days < 1 || days > MAX_MANDATE_DAYS) return fail(400, 'validation', `Pick a length from 1 to ${MAX_MANDATE_DAYS} days.`)
    const kids = this.tickets(ws)
    if (kids.length < 6) return fail(400, 'validation', 'This workspace has too few tickets for the preview.')
    const prefix = this.store.workspaces.find((w) => w.id === ws)?.prefix ?? ws
    const now = Date.parse(this.store.now())
    const issued = now - 3 * DAY
    const kid = (i: number) => kids[i % kids.length]
    // Ten decisions in the order they happened: gate approvals, verdicts (two on work that landed), a code review, an
    // unblocked ticket, a factory permit, a factory enabled, a full run started and a grant that started an agent.
    const plan: { k?: number; kind: MandateDecisionKind; landed?: boolean; target?: string }[] = [
      { k: 0, kind: 'requirements' }, { k: 0, kind: 'plan' }, { k: 0, kind: 'verdict', landed: true },
      { k: 4, kind: 'unblock' },
      { k: 2, kind: 'verdict', landed: true }, { k: 2, kind: 'code_review' },
      { kind: 'factory_enabled', target: `AI Factory in ${prefix}` },
      { k: 1, kind: 'permit', target: 'uv run pytest tests/billing -q' },
      { kind: 'factory_run', target: 'full run R-3 “Tariff page copy”, up to Preview' },
      { kind: 'grant', target: 'grant for 8 h, started Claude Code on the next ticket' },
    ]
    const commits = ['b7e1f02', '4c9a3d1', 'e02f7b8']
    let c = 0
    const md = `md_${s.next}`
    const checker = 'si_chk_4f2a'
    const decisions: MandateDecision[] = plan.map((p, i) => {
      const t = p.k === undefined ? undefined : kid(p.k)
      return {
        seq: FIRST_SEQ + i,
        id: `${md}.d${i + 1}`,
        ...(t ? { ticket: t.key, title: t.title } : {}),
        ...(p.target ? { target: p.target } : {}),
        kind: p.kind,
        ...(p.kind === 'verdict' || p.kind === 'code_review' ? { commit: commits[c++ % commits.length] } : {}),
        at: iso(issued + (i + 1) * 6 * 3_600_000),
        checker: { identity: checker, result: 'passed' as const },
        landed: !!p.landed,
        // Looked at on earlier days: the first five. The rest is new since your last look.
        ...(i < 5 ? { review: 'looks_right' as const } : {}),
      }
    })
    const refused: MandateRefusal[] = [
      {
        id: `${md}.r1`, ticket: kid(1).key, title: kid(1).title, kind: 'verdict', reason: 'protected_path',
        detail: 'Verdict refused: the diff touches package-lock.json (a dependency lockfile, protected). A person approves this.',
        at: iso(now - 9 * 3_600_000),
      },
      {
        id: `${md}.r2`, ticket: kid(3).key, title: kid(3).title, kind: 'plan', reason: 'veto',
        detail: 'Plan approval skipped: you requested changes on this plan. Your “no” stands until you lift it.',
        at: iso(now - 5 * 3_600_000),
      },
      {
        id: `${md}.r3`, asked: 'Install the addon drop', reason: 'always_human',
        detail: 'Refused: addons are always yours. The orchestrator asked to install drop for a handoff; a person installs addons.',
        at: iso(now - 3 * 3_600_000),
      },
      {
        id: `${md}.r4`, asked: 'Issue a mandate to a second orchestrator', reason: 'chain',
        detail: 'Refused (mandate.chain_refused): a mandate never issues another mandate. Grants that start agents are allowed.',
        at: iso(now - 2 * 3_600_000),
      },
    ]
    return {
      id: md,
      revision: 1,
      issuer: this.name(ws, this.store.viewer),
      orchestrator: { name: orchestrator.name, identity: 'si_orc_91c3' },
      checker: { identity: checker },
      scope: 'workspace',
      days,
      issued_at: iso(issued),
      expires: iso(issued + days * DAY),
      state: 'active',
      limits: {
        decisions: { used: decisions.length, max: 200 },
        grants: { used: decisions.filter((d) => d.kind === 'grant').length, max: 20 },
      },
      decisions,
      refused,
      revisions: [{ revision: 1, at: iso(issued), what: `Issued on this Mac with Touch ID (preview: nothing was signed). Whole workspace, ${days} ${days === 1 ? 'day' : 'days'}, renewable.` }],
    }
  }

  request(ws: string, body: MandatesPreviewRequest | null): { ok: true; state: MandatesPreviewState } | StoreFailure {
    if (!this.store.workspaces.some((w) => w.id === ws)) return fail(404, 'not_found', 'No such workspace')
    if (!can(this.store.roleIn(ws, this.store.viewer), 'settings')) return fail(403, 'forbidden', 'Only owners can try the mandates preview.', 'Ask an owner.')
    const s = this.sim(ws)
    const m = s.mandate
    switch (body?.op) {
      case 'enable': {
        s.on = true
        if (body.seed && (!m || m.state === 'revoked')) {
          const orch = this.orchestrators(ws)[0] ?? { name: 'Claude Code', session: 's_orc' }
          const seeded = this.seedMandate(ws, s, orch, DEFAULT_DAYS)
          if ('ok' in seeded) return seeded
          s.mandate = seeded
        }
        break
      }
      case 'disable':
        s.on = false
        s.mandate = null
        break
      case 'issue': {
        if (!s.on) return fail(409, 'conflict', 'Turn the preview on first.', 'Use “Show the mandate anyway (preview)”.')
        if (m && m.state !== 'revoked' && m.state !== 'stopped') return fail(409, 'conflict', `Mandate ${m.id} is still in force.`, 'One mandate at a time in the preview: stop it first.')
        const orch = this.orchestrators(ws).find((o) => o.session === body.orchestrator)
        if (!orch) return fail(400, 'validation', 'Pick the orchestrator.')
        if (m) s.next += 1
        const seeded = this.seedMandate(ws, s, orch, body.days)
        if ('ok' in seeded) {
          if (m) s.next -= 1
          return seeded
        }
        s.mandate = seeded
        break
      }
      case 'renew': {
        // Renewing is one new signature on the Mac: a new revision with a new length from now (at most 30 days).
        if (!m || m.state !== 'active') return fail(409, 'conflict', 'No mandate in force to renew.', 'A stopped or revoked mandate is not renewed: issue a new one.')
        if (!Number.isInteger(body.days) || body.days < 1 || body.days > MAX_MANDATE_DAYS) return fail(400, 'validation', `Pick a length from 1 to ${MAX_MANDATE_DAYS} days.`)
        const now = this.store.now()
        m.revision += 1
        m.days = body.days
        m.expires = iso(Date.parse(now) + body.days * DAY)
        m.revisions.push({ revision: m.revision, at: now, what: `Renewed on this Mac with Touch ID (preview: nothing was signed). ${body.days} ${body.days === 1 ? 'day' : 'days'} from now; same scope, lifetime counters kept.` })
        break
      }
      case 'stop':
        if (!m || m.state !== 'active') return fail(409, 'conflict', 'No mandate in force to stop.')
        m.state = 'stopping'
        m.stop = { requested_at: this.store.now(), stop_agents: !!body.stop_agents }
        break
      case 'review': {
        const d = m?.decisions.find((x) => x.id === body.decision)
        if (!m || !d) return fail(404, 'not_found', 'No such decision')
        if (d.voided) return fail(409, 'conflict', 'That decision was voided.')
        if (body.review !== 'looks_right' && body.review !== 'veto') return fail(400, 'validation', 'review must be looks_right or veto')
        d.review = body.review
        break
      }
      case 'revoke':
        if (!m || m.state === 'revoked') return fail(409, 'conflict', 'No mandate to revoke.')
        for (const d of m.decisions) if (!d.landed) d.voided = true
        m.state = 'revoked'
        m.revoked_at = this.store.now()
        if (m.stop && !m.stop.boundary_seq) m.stop.boundary_seq = BOUNDARY_SEQ
        break
      default:
        return fail(400, 'validation', 'Unknown preview op')
    }
    this.save()
    return { ok: true, state: this.state(ws) }
  }
}
