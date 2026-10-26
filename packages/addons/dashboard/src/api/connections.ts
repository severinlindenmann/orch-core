// Skills, connections and simple auth (D55–D57; spec ux/spec-d53-d57.md §3–6). Part of the API contract: the zod
// schemas the client parses every answer with, and the pure rules both sides use. Secret VALUES never appear here:
// the host answers with names, paths and permission status only.
import { z } from 'zod'

/** A connection or skill name: lower case, digits and dashes (`databricks-prod`). */
export const NAME_RE = /^[a-z][a-z0-9-]{0,62}$/
/** An environment variable name as the secrets file and the sidecar spell it (`DATABRICKS_TOKEN`). */
export const ENV_RE = /^[A-Z][A-Z0-9_]{0,63}$/

const name = z.string().regex(NAME_RE)
const envName = z.string().regex(ENV_RE)
const iso = z.string().max(40)
const line = z.string().max(400)

// ---------------------------------------------------------------- skills

/** `orch.skill.json`, the sidecar next to SKILL.md (frontmatter carries only name and description). Strict: unknown keys refuse. */
export const skillSidecar = z.strictObject({
  schema_version: z.literal(1),
  skill_version: z.string().regex(/^\d+\.\d+\.\d+$/),
  scope: z.enum(['built_in', 'workspace', 'org']),
  connections: z.array(name).max(20),
  env: z.array(envName).max(20),
})
export type SkillSidecar = z.output<typeof skillSidecar>

export const SKILL_SCOPES = ['built_in', 'workspace', 'org'] as const
export type SkillScope = (typeof SKILL_SCOPES)[number]
export const SCOPE_LABEL: Record<SkillScope, string> = { built_in: 'Built in', workspace: 'Workspace', org: 'Org' }

/** One connection or env reference of a skill: in effect only once the owner granted it. */
const reference = z.object({ name: z.string().max(64), granted: z.boolean() })

export const skillGrant = z.object({
  at: iso,
  by: z.string().max(64),
  connections: z.array(name),
  env: z.array(envName),
  presence: z.literal('touchid'),
})
export type SkillGrant = z.output<typeof skillGrant>

export const skillInfo = z.object({
  name,
  description: z.string().max(1024),
  scope: z.enum(SKILL_SCOPES),
  /** Where SKILL.md lives (repo path, or the package for built-in skills). */
  path: z.string().max(200),
  /** Built-in skills come from orch-core or an addon. */
  source: z.string().max(64),
  skill_md: z.string().max(20_000),
  /** The sidecar as it is on disk (pretty JSON); null: there is none, so the needs are unknown. */
  sidecar: z.string().max(4000).nullable(),
  /** `declared`: a valid sidecar; `unknown`: no sidecar (doctor warns); `invalid`: a sidecar that does not parse. */
  needs: z.enum(['declared', 'unknown', 'invalid']),
  version: z.string().max(40).nullable(),
  connections: z.array(reference),
  env: z.array(reference.extend({ in_secrets_file: z.boolean() })),
  grants: z.array(skillGrant),
  /** Tickets (visible to the viewer) that use this skill. */
  tickets: z.array(z.string().max(40)),
  /** needs `invalid`: why orch.skill.json does not validate. */
  sidecar_problem: z.string().max(300).optional(),
  /** Where a reference that waits for a grant came from (a commit, an agent). */
  pending_note: z.string().max(400).optional(),
})
export type SkillInfo = z.output<typeof skillInfo>
export const skillList = z.array(skillInfo)

// ---------------------------------------------------------------- connections

export const CHECK_STATUSES = ['ok', 'auth_expired', 'wrong_identity', 'service_down', 'unknown'] as const
export type CheckStatus = (typeof CHECK_STATUSES)[number]
export const CHECK_LABEL: Record<CheckStatus, string> = {
  ok: 'ok',
  auth_expired: 'auth expired',
  wrong_identity: 'wrong identity',
  service_down: 'service down',
  unknown: 'unknown',
}
/** Only these block work, and only on tickets that need the connection (D57). */
export const BLOCKING: readonly CheckStatus[] = ['auth_expired', 'wrong_identity']
export const isBlocking = (s: CheckStatus) => BLOCKING.includes(s)

export const CHECK_TRIGGERS = ['on_demand', 'doctor', 'session_start', 'claim', 'relogin'] as const
export type CheckTrigger = (typeof CHECK_TRIGGERS)[number]

export const checkResult = z.object({
  status: z.enum(CHECK_STATUSES),
  at: iso,
  trigger: z.enum(CHECK_TRIGGERS),
  /** The command run (closed stdin, timeout, bounded output). */
  command: line,
  timeout_s: z.number(),
  duration_ms: z.number(),
  exit_code: z.number().nullable(),
  /** Bounded and filtered for secret values before it left the host. */
  output: z.array(line).max(20),
  truncated: z.boolean(),
  /** wrong identity: what the connection names and what the check reported. */
  expected: z.string().max(200).optional(),
  actual: z.string().max(200).optional(),
})
export type CheckResult = z.output<typeof checkResult>

export const connectionInfo = z.object({
  name,
  kind: z.enum(['cli_login', 'api_token']),
  tool: z.string().max(40),
  /** The identity it is bound to: one of account / tenant / endpoint / profile. */
  target: z.object({ label: z.enum(['account', 'tenant', 'endpoint', 'profile']), value: z.string().max(200) }),
  check: line,
  /** CLI logins: the command the owner runs to log in again. API tokens: none (edit the secrets file). */
  login_hint: line.nullable(),
  /** The OS user the login must exist for (agents run as their own user from P2). */
  run_as: z.string().max(64),
  /** API tokens: the secrets-file names it reads. */
  env: z.array(envName),
  last_check: checkResult.nullable(),
  skills: z.array(name),
  /** Tickets (visible to the viewer) whose skills need it; `blocked` when its last check is auth or identity. */
  tickets: z.array(z.string().max(40)),
})
export type ConnectionInfo = z.output<typeof connectionInfo>
export const connectionList = z.array(connectionInfo)

// ---------------------------------------------------------------- the secrets file (names only)

export const staleSession = z.object({
  session: z.string().max(64),
  agent: z.string().max(40),
  ticket: z.string().max(40),
  started: iso,
})
export type StaleSession = z.output<typeof staleSession>

export const secretsFileInfo = z.object({
  /** False: no secrets file for this workspace yet (no API-token connections). */
  exists: z.boolean(),
  /** `<host state dir>/secrets/<workspace-uuid>.env`, and that path resolved on this host. */
  template: z.string().max(200),
  path: z.string().max(300),
  owner_user: z.string().max(64),
  dir_mode: z.string().max(8),
  file_mode: z.string().max(8),
  /** Directory 0700, file 0600, owned by the user that runs the agents. */
  permissions_ok: z.boolean(),
  modified_at: iso,
  names: z.array(z.object({ name: envName, line: z.number(), skills: z.array(name), connections: z.array(name) })),
  /** Lines the parser refused (line number and reason; never the line's text). */
  problems: z.array(z.object({ line: z.number(), reason: line })),
  /** Running agent sessions started before the file's last change: they keep the old values until restarted. */
  stale_sessions: z.array(staleSession),
})
export type SecretsFileInfo = z.output<typeof secretsFileInfo>

// ---------------------------------------------------------------- doctor

export const doctorReport = z.object({
  at: iso,
  checks: z.array(z.object({ name, status: z.enum(CHECK_STATUSES), summary: line })),
  unknown_skills: z.array(z.object({ name, scope: z.enum(SKILL_SCOPES), path: z.string().max(200), needs: z.enum(['unknown', 'invalid']) })),
  ungranted: z.array(z.object({ skill: name, refs: z.array(z.string().max(64)) })),
  secrets_permissions_ok: z.boolean(),
  /** Refused lines of the secrets file (number and reason). */
  secrets_problems: z.array(z.object({ line: z.number(), reason: line })),
  stale_sessions: z.array(staleSession),
})
export type DoctorReport = z.output<typeof doctorReport>

// ---------------------------------------------------------------- what a ticket needs (core-computed, on the ticket document)

export interface TicketNeeds {
  skills: { name: string; needs: 'declared' | 'unknown' | 'invalid' }[]
  connections: { name: string; status: CheckStatus }[]
  /** Env names a session on this ticket gets (granted, declared by its skills, present in the secrets file). */
  env: string[]
  /** The first needed connection whose last check is auth expired or wrong identity; claim and start refuse. */
  blocked: { connection: string; status: 'auth_expired' | 'wrong_identity' } | null
}

/** "Blocked: databricks-prod auth expired" — the one sentence every surface uses. */
export const blockedText = (b: NonNullable<TicketNeeds['blocked']>) => `Blocked: ${b.connection} ${CHECK_LABEL[b.status]}`

/** A credential grant request: connection references and env names to add to a skill. */
export interface SkillGrantRequest {
  connections: string[]
  env: string[]
  /** Core's dialog was shown and signed (the host refuses without it). */
  confirmed: true
}
