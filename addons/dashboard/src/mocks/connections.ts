// Host side of skills, connections and simple auth (D55–D57). Core, not an addon: the store owns one instance.
// - Skills come from the fixture (built in, and per workspace); grants are the fixture history plus
//   `skill.credentials_granted` workspace events (the audit record of each signed grant).
// - Connections are workspace config; their last check result is the fixture's seed check, overridden by
//   `connection.checked` workspace events. A check is simulated from the connection's `sim` state; once the owner
//   re-logged in (a `relogin` check), a CLI login checks ok from then on.
// - The secrets file is parsed here and its VALUES never leave this module: answers carry names, the path and the
//   permission status only, and every check output is filtered for the values before it is returned or recorded.
import { addonActive } from '@/api/addons'
import {
  BLOCKING,
  CHECK_LABEL,
  ENV_RE,
  skillSidecar,
  type SkillSidecar,
  isBlocking,
  type CheckResult,
  type CheckStatus,
  type CheckTrigger,
  type ConnectionInfo,
  type DoctorReport,
  type SecretsFileInfo,
  type SkillGrant,
  type SkillInfo,
  type SkillScope,
  type StaleSession,
  type TicketNeeds,
} from '@/api/connections'
import { maskSecrets, parseSecretsFile } from '@/api/secrets'
import type { Actor, WorkspaceEvent } from '@/api/types'
import connectionsFixture from './fixtures/connections.json'
import skillsFixture from './fixtures/skills.json'
import type { MockStore, StoreFailure } from './store'

/** Template of the secrets file path (spec §4 B). */
export const SECRETS_TEMPLATE = '<host state dir>/secrets/<workspace-uuid>.env'
/** Check limits (spec §5): timeout, closed stdin, bounded output. */
export const CHECK_TIMEOUT_S = 10
const MAX_LINES = 12
const MAX_LINE = 200

interface Sim {
  status: CheckStatus
  exit_code: number | null
  duration_ms: number
  output: string[]
  actual?: string
}
interface FixtureConnection {
  name: string
  kind: 'cli_login' | 'api_token'
  tool: string
  target: { label: 'account' | 'tenant' | 'endpoint' | 'profile'; value: string }
  check: string
  login_hint: string | null
  env: string[]
  sim: Sim
  ok_output?: string[]
  seed_check: { at: string; trigger: CheckTrigger }
}
interface FixtureWorkspace {
  run_as: string
  connections: FixtureConnection[]
  secrets: { path: string; owner_user: string; dir_mode: string; file_mode: string; modified_at: string; text: string }
  ticket_skills: Record<string, string[]>
}
interface FixtureSkill {
  name: string
  description: string
  path: string
  source?: string
  addon?: string
  skill_md: string
  /** As on disk: validated with `skillSidecar` (strict) when the host reads it. */
  sidecar: unknown
  grants?: SkillGrant[]
  pending_note?: string
}

const CONFIG = connectionsFixture as unknown as Record<string, FixtureWorkspace>
const SKILLS = skillsFixture as unknown as { built_in: FixtureSkill[]; workspace: Record<string, FixtureSkill[]> }

const refuse = (status: number, code: string, message: string, hint?: string): StoreFailure => ({ ok: false, status, code, message, hint })
const uniq = <T,>(xs: T[]) => [...new Set(xs)]

/** A skill as the host knows it: fixture plus folded grants. */
interface HostSkill {
  name: string
  description: string
  scope: SkillScope
  path: string
  source: string
  skill_md: string
  /** The sidecar when it is valid; null when there is none or it does not validate. */
  sidecar: SkillSidecar | null
  /** What is on disk (shown as is), and why it does not validate. */
  raw: unknown
  problem?: string
  grants: SkillGrant[]
  pending_note?: string
}

export class ConnectionsHost {
  constructor(private store: MockStore) {}

  /** Derived per workspace log (array identity + length): ticket documents ask for needs on every read. */
  private memo = new WeakMap<WorkspaceEvent[], { len: number; skills: HostSkill[]; last: Map<string, CheckResult> }>()
  private derived(wsId: string): { skills: HostSkill[]; last: Map<string, CheckResult> } {
    const log = this.store.wsEventsOf(wsId)
    const hit = this.memo.get(log)
    if (hit && hit.len === log.length) return hit
    const fresh = { len: log.length, skills: this.foldSkills(wsId), last: this.foldChecks(wsId) }
    this.memo.set(log, fresh)
    return fresh
  }

  private config(wsId: string): FixtureWorkspace | undefined {
    const prefix = this.store.workspaces.find((w) => w.id === wsId)?.prefix
    return prefix ? CONFIG[prefix] : undefined
  }
  private events(wsId: string, type: string): WorkspaceEvent[] {
    return this.store.wsEventsOf(wsId).filter((e) => e.type === type)
  }

  // ------------------------------------------------------------ secrets (values stay here)

  private secretValues(wsId: string): { name: string; value: string }[] {
    const text = this.config(wsId)?.secrets.text
    return text ? parseSecretsFile(text).entries.map(({ name, value }) => ({ name, value })) : []
  }
  private secretNames(wsId: string): string[] {
    return this.secretValues(wsId).map((s) => s.name)
  }
  /** The output filter: known secret values become `•••• (NAME)`. */
  filter(wsId: string, text: string): string {
    return maskSecrets(text, this.secretValues(wsId))
  }

  // ------------------------------------------------------------ skills

  private hostSkills(wsId: string): HostSkill[] {
    return this.derived(wsId).skills
  }
  private foldSkills(wsId: string): HostSkill[] {
    const w = this.store.workspaces.find((x) => x.id === wsId)
    if (!w) return []
    const builtIn = SKILLS.built_in.filter((s) => !s.addon || addonActive(w, s.addon))
    const own = SKILLS.workspace[w.prefix] ?? []
    const granted = this.events(wsId, 'skill.credentials_granted')
    return [
      ...builtIn.map((s) => ({ ...this.base(s, 'built_in'), source: s.source ?? 'orch-core' })),
      ...own.map((s) => {
        const b = this.base(s, 'workspace')
        const mine = granted.filter((e) => e.skill === s.name)
        const grants = [
          ...b.grants,
          ...mine.map((e): SkillGrant => ({ at: e.at, by: e.actor.id, connections: (e.connections as string[]) ?? [], env: (e.env as string[]) ?? [], presence: 'touchid' })),
        ]
        // A grant of a name the sidecar does not list yet adds it there (orch writes orch.skill.json).
        let sidecar = b.sidecar ? structuredClone(b.sidecar) : null
        // An invalid sidecar is never rewritten (the grant path refuses it): fix the file first.
        if (!b.problem) for (const e of mine) {
          sidecar ??= { schema_version: 1, skill_version: '0.1.0', scope: 'workspace', connections: [], env: [] }
          sidecar.connections = uniq([...sidecar.connections, ...((e.connections as string[]) ?? [])])
          sidecar.env = uniq([...sidecar.env, ...((e.env as string[]) ?? [])])
        }
        return { ...b, source: 'workspace repo', sidecar, grants }
      }),
    ]
  }
  private base(s: FixtureSkill, scope: SkillScope): HostSkill {
    const parsed = s.sidecar == null ? null : skillSidecar.safeParse(s.sidecar)
    const problem = parsed && !parsed.success ? parsed.error.issues.map((i) => `${i.path.join('.') || 'file'}: ${i.message}`).join('; ').slice(0, 300) : undefined
    return {
      name: s.name,
      description: s.description,
      scope,
      path: s.path,
      source: '',
      skill_md: s.skill_md,
      sidecar: parsed?.success ? structuredClone(parsed.data) : null,
      raw: s.sidecar ?? null,
      ...(problem ? { problem } : {}),
      grants: structuredClone(s.grants ?? []),
      pending_note: s.pending_note,
    }
  }
  /** A reference is in effect once granted; a built-in skill's references come with the release (or its addon's grant). */
  private grantedRefs(s: HostSkill): { connections: Set<string>; env: Set<string> } {
    const listed = { connections: s.sidecar?.connections ?? [], env: s.sidecar?.env ?? [] }
    if (s.scope === 'built_in') return { connections: new Set(listed.connections), env: new Set(listed.env) }
    // In effect: granted AND still in the sidecar (a reference removed from orch.skill.json stops counting).
    const granted = { connections: new Set(s.grants.flatMap((g) => g.connections)), env: new Set(s.grants.flatMap((g) => g.env)) }
    return { connections: new Set(listed.connections.filter((n) => granted.connections.has(n))), env: new Set(listed.env.filter((n) => granted.env.has(n))) }
  }
  private needsOf(s: HostSkill): 'declared' | 'unknown' | 'invalid' {
    return s.problem ? 'invalid' : s.sidecar ? 'declared' : 'unknown'
  }

  /** Skill keys used by a ticket (its `skills` list). */
  ticketSkills(wsId: string, key: string): string[] {
    return this.config(wsId)?.ticket_skills[key] ?? []
  }
  private visibleTickets(wsId: string, uses: (key: string) => boolean): string[] {
    const keys = Object.keys(this.config(wsId)?.ticket_skills ?? {})
    return keys.filter((k) => this.store.hasTicket(k) && this.store.workspaceOf(k)?.id === wsId && this.store.isVisible(k) && uses(k)).sort()
  }

  skills(wsId: string): SkillInfo[] {
    const names = new Set(this.secretNames(wsId))
    return this.hostSkills(wsId).map((s) => {
      const g = this.grantedRefs(s)
      return {
        name: s.name,
        description: s.description,
        scope: s.scope,
        path: s.path,
        source: s.source,
        skill_md: s.skill_md,
        sidecar: s.sidecar ? JSON.stringify(s.sidecar, null, 2) : s.raw != null ? JSON.stringify(s.raw, null, 2) : null,
        ...(s.problem ? { sidecar_problem: s.problem } : {}),
        needs: this.needsOf(s),
        version: s.sidecar?.skill_version ?? null,
        connections: (s.sidecar?.connections ?? []).map((n) => ({ name: n, granted: g.connections.has(n) })),
        env: (s.sidecar?.env ?? []).map((n) => ({ name: n, granted: g.env.has(n), in_secrets_file: names.has(n) })),
        grants: s.grants,
        tickets: this.visibleTickets(wsId, (k) => this.ticketSkills(wsId, k).includes(s.name)),
        ...(s.pending_note ? { pending_note: s.pending_note } : {}),
      }
    })
  }

  /** Owner, signed in core's dialog: add connection references and env names to a workspace skill. */
  grant(wsId: string, skillName: string, body: unknown, actor: Actor): { ok: true; skill: SkillInfo } | StoreFailure {
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Only a person grants credentials.', 'Agents never grant.')
    if (this.store.roleIn(wsId, actor.id) !== 'owner') return refuse(403, 'forbidden', 'Only owners grant credentials to skills.', 'Ask an owner.')
    const b = (body ?? {}) as { connections?: unknown; env?: unknown; confirmed?: unknown }
    if (b.confirmed !== true) return refuse(409, 'confirm.required', 'A credential grant is signed in the dashboard.', 'Sign it in the grant dialog.')
    const skill = this.hostSkills(wsId).find((s) => s.name === skillName)
    if (!skill) return refuse(404, 'not_found', `No skill ${skillName}`)
    if (skill.scope === 'built_in') return refuse(409, 'skill.built_in', `${skillName} is built in: its needs change with a release.`, 'Copy it into the workspace repo to change what it uses.')
    if (skill.problem) return refuse(409, 'skill.invalid_sidecar', `${skillName}'s orch.skill.json does not validate.`, 'Fix the file in the workspace repo first.')
    const conns = Array.isArray(b.connections) ? b.connections : []
    const env = Array.isArray(b.env) ? b.env : []
    const known = new Set((this.config(wsId)?.connections ?? []).map((c) => c.name))
    const badConn = conns.find((c) => typeof c !== 'string' || !known.has(c))
    if (badConn !== undefined) return refuse(400, 'validation.connection', `No connection ${String(badConn)} in this workspace.`, 'Add it to the workspace config first.')
    const badEnv = env.find((e) => typeof e !== 'string' || !ENV_RE.test(e))
    if (badEnv !== undefined) return refuse(400, 'validation.env', `${String(badEnv)} is not an environment name.`, 'Upper case letters, digits and _, starting with a letter.')
    if (!conns.length && !env.length) return refuse(400, 'validation', 'Pick a connection or an env name to grant.')
    const g = this.grantedRefs(skill)
    const newConns = uniq(conns as string[]).filter((c) => !g.connections.has(c))
    const newEnv = uniq(env as string[]).filter((e) => !g.env.has(e))
    if (!newConns.length && !newEnv.length) return refuse(409, 'skill.already_granted', `${skillName} already has everything you picked.`)
    this.store.appendWs(wsId, { type: 'skill.credentials_granted', actor, skill: skillName, connections: newConns, env: newEnv, presence: 'touchid' })
    return { ok: true, skill: this.skills(wsId).find((s) => s.name === skillName)! }
  }

  // ------------------------------------------------------------ connections and checks

  private lastChecks(wsId: string): Map<string, CheckResult> {
    return this.derived(wsId).last
  }
  private foldChecks(wsId: string): Map<string, CheckResult> {
    const out = new Map<string, CheckResult>()
    for (const c of this.config(wsId)?.connections ?? []) out.set(c.name, this.simulate(wsId, c, c.seed_check.trigger, c.seed_check.at, false))
    for (const e of this.events(wsId, 'connection.checked')) if (typeof e.name === 'string' && e.result) out.set(e.name, e.result as CheckResult)
    return out
  }
  private reloggedIn(wsId: string, name: string): boolean {
    return this.events(wsId, 'connection.checked').some((e) => e.name === name && (e.result as CheckResult | undefined)?.trigger === 'relogin')
  }

  /** Run the simulated check: classify, bound and filter the output. Nothing here is recorded. */
  private simulate(wsId: string, c: FixtureConnection, trigger: CheckTrigger, at: string, relogged = this.reloggedIn(wsId, c.name)): CheckResult {
    const fixed = c.kind === 'cli_login' && isBlocking(c.sim.status) && (relogged || trigger === 'relogin')
    const status: CheckStatus = fixed ? 'ok' : c.sim.status
    const raw = fixed ? (c.ok_output ?? ['ok']) : c.sim.output
    // The tool's output as the host sees it (placeholders stand for the values the process was given), then the filter.
    const values = this.secretValues(wsId)
    const lines = raw.map((l) => this.filter(wsId, l.replace(/\{([A-Z][A-Z0-9_]*)\}/g, (m, n: string) => values.find((v) => v.name === n)?.value ?? m)))
    const bounded = lines.slice(0, MAX_LINES).map((l) => (l.length > MAX_LINE ? l.slice(0, MAX_LINE) + '…' : l))
    return {
      status,
      at,
      trigger,
      command: c.check,
      timeout_s: CHECK_TIMEOUT_S,
      duration_ms: fixed ? 700 : c.sim.duration_ms,
      exit_code: fixed ? 0 : c.sim.exit_code,
      output: bounded,
      truncated: lines.length > MAX_LINES || lines.some((l) => l.length > MAX_LINE),
      ...(status === 'wrong_identity' ? { expected: c.target.value, actual: c.sim.actual ?? 'another account' } : {}),
    }
  }

  connections(wsId: string): ConnectionInfo[] {
    const cfg = this.config(wsId)
    if (!cfg) return []
    const last = this.lastChecks(wsId)
    const skills = this.hostSkills(wsId)
    return cfg.connections.map((c) => {
      const usedBy = skills.filter((s) => this.grantedRefs(s).connections.has(c.name)).map((s) => s.name)
      return {
        name: c.name,
        kind: c.kind,
        tool: c.tool,
        target: c.target,
        check: c.check,
        login_hint: c.login_hint,
        run_as: cfg.run_as,
        env: c.env,
        last_check: last.get(c.name) ?? null,
        skills: usedBy,
        tickets: this.visibleTickets(wsId, (k) => this.ticketSkills(wsId, k).some((s) => usedBy.includes(s))),
      }
    })
  }

  /** Run one connection's check and record it (`connection.checked`, by the host). `relogin` is the owner's "Run check again" after logging in. */
  check(wsId: string, name: string, trigger: CheckTrigger, actor: Actor): { ok: true; connection: ConnectionInfo } | StoreFailure {
    const role = this.store.roleIn(wsId, actor.id)
    if (actor.kind !== 'person') return refuse(403, 'human_only', 'Checks are run by orch or by a person.')
    if (!role || role === 'viewer') return refuse(403, 'forbidden', 'Viewers cannot run checks.', 'Ask a member.')
    const c = this.config(wsId)?.connections.find((x) => x.name === name)
    if (!c) return refuse(404, 'not_found', `No connection ${name}`)
    if (trigger === 'relogin') {
      if (role !== 'owner') return refuse(403, 'forbidden', 'Re-login is the owner\'s: agents and other members never handle logins.')
      if (c.kind !== 'cli_login') return refuse(409, 'connection.no_login', `${name} uses an API token: edit the secrets file instead.`)
    }
    this.record(wsId, c, trigger)
    return { ok: true, connection: this.connections(wsId).find((x) => x.name === name)! }
  }
  private record(wsId: string, c: FixtureConnection, trigger: CheckTrigger): CheckResult {
    const result = this.simulate(wsId, c, trigger, this.store.now())
    this.store.appendWs(wsId, { type: 'connection.checked', actor: { kind: 'host', id: 'orch' }, name: c.name, result })
    return result
  }

  /** `orch doctor`: every check, skills with unknown needs, ungranted references, the secrets file. */
  doctor(wsId: string, actor: Actor): { ok: true; report: DoctorReport } | StoreFailure {
    const role = this.store.roleIn(wsId, actor.id)
    if (!role || role === 'viewer') return refuse(403, 'forbidden', 'Viewers cannot run the doctor.', 'Ask a member.')
    const checks = (this.config(wsId)?.connections ?? []).map((c) => {
      const r = this.record(wsId, c, 'doctor')
      return { name: c.name, status: r.status, summary: r.status === 'wrong_identity' ? `expected ${r.expected}, got ${r.actual}` : (r.output[0] ?? CHECK_LABEL[r.status]) }
    })
    const skills = this.skills(wsId)
    const secrets = this.secretsFile(wsId)
    return {
      ok: true,
      report: {
        at: this.store.now(),
        checks,
        unknown_skills: skills.flatMap((s) => (s.needs === 'declared' ? [] : [{ name: s.name, scope: s.scope, path: s.path, needs: s.needs }])),
        ungranted: skills.flatMap((s) => {
          const refs = [...s.connections.filter((r) => !r.granted).map((r) => r.name), ...s.env.filter((r) => !r.granted).map((r) => r.name)]
          return refs.length ? [{ skill: s.name, refs }] : []
        }),
        secrets_permissions_ok: secrets.permissions_ok,
        secrets_problems: secrets.problems,
        stale_sessions: secrets.stale_sessions,
      },
    }
  }

  // ------------------------------------------------------------ the secrets file (names only)

  secretsFile(wsId: string): SecretsFileInfo {
    const cfg = this.config(wsId)
    const skills = this.hostSkills(wsId)
    if (!cfg) {
      return { exists: false, template: SECRETS_TEMPLATE, path: `~/.local/state/orch/secrets/${wsId}.env`, owner_user: 'orch-agent', dir_mode: '0700', file_mode: '0600', permissions_ok: true, modified_at: '', names: [], problems: [], stale_sessions: [] }
    }
    const parsed = parseSecretsFile(cfg.secrets.text)
    const s = cfg.secrets
    return {
      exists: true,
      template: SECRETS_TEMPLATE,
      path: s.path,
      owner_user: s.owner_user,
      dir_mode: s.dir_mode,
      file_mode: s.file_mode,
      permissions_ok: s.dir_mode === '0700' && s.file_mode === '0600' && s.owner_user === cfg.run_as,
      modified_at: s.modified_at,
      names: parsed.entries.map((e) => ({
        name: e.name,
        line: e.line,
        skills: skills.filter((k) => this.grantedRefs(k).env.has(e.name)).map((k) => k.name),
        connections: cfg.connections.filter((c) => c.env.includes(e.name)).map((c) => c.name),
      })),
      problems: parsed.problems,
      stale_sessions: this.staleSessions(wsId, s.modified_at),
    }
  }

  /** Running sessions on tickets that get secret values, started before the file's last change. */
  private staleSessions(wsId: string, modified: string): StaleSession[] {
    const out: StaleSession[] = []
    for (const a of this.store.agents(wsId)) {
      if (a.parent || a.state === 'stopped') continue
      for (const c of a.claims) {
        if (c.since >= modified || !this.needs(wsId, c.ticket)?.env.length) continue
        out.push({ session: a.session, agent: a.name, ticket: c.ticket, started: c.since })
      }
    }
    return out
  }

  // ------------------------------------------------------------ what a ticket needs (on the ticket document)

  needs(wsId: string, key: string): TicketNeeds | undefined {
    const names = this.ticketSkills(wsId, key)
    if (!names.length) return undefined
    const all = this.hostSkills(wsId)
    const skills = names.flatMap((n) => all.filter((s) => s.name === n))
    const last = this.lastChecks(wsId)
    const secrets = new Set(this.secretNames(wsId))
    const conns = uniq(skills.flatMap((s) => [...this.grantedRefs(s).connections]))
    const connections = conns.map((n) => ({ name: n, status: last.get(n)?.status ?? ('unknown' as CheckStatus) }))
    const failing = connections.find((c) => BLOCKING.includes(c.status))
    return {
      skills: skills.map((s) => ({ name: s.name, needs: this.needsOf(s) })),
      connections,
      env: uniq(skills.flatMap((s) => [...this.grantedRefs(s).env])).filter((n) => secrets.has(n)).sort(),
      blocked: failing ? { connection: failing.name, status: failing.status as 'auth_expired' | 'wrong_identity' } : null,
    }
  }

  /**
   * Before a claim or a session start (D57): run the checks of the connections the ticket needs, then refuse when one
   * fails with auth or identity. A check whose result did not change is not recorded again (a refusal writes nothing new).
   */
  precheck(wsId: string, key: string, trigger: 'claim' | 'session_start'): StoreFailure | null {
    const n = this.needs(wsId, key)
    if (!n) return null
    const last = this.lastChecks(wsId)
    for (const c of this.config(wsId)?.connections ?? []) {
      if (!n.connections.some((x) => x.name === c.name)) continue
      const r = this.simulate(wsId, c, trigger, this.store.now())
      if (r.status !== last.get(c.name)?.status) this.store.appendWs(wsId, { type: 'connection.checked', actor: { kind: 'host', id: 'orch' }, name: c.name, result: r })
    }
    const b = this.needs(wsId, key)?.blocked
    if (!b) return null
    return refuse(409, 'connection.blocked', `${key} is blocked: ${b.connection} ${CHECK_LABEL[b.status]}.`, 'An owner logs in again (Today or Settings > Connections); agents never handle logins.')
  }
}
