// Settings › Connections (D55–D57): named tools and APIs bound to one identity, their checks, the doctor and the
// secrets file. Values never reach the dashboard: the host answers with names, the path and permission status.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ChevronDown, FileLock2, Loader2, Play, ShieldAlert, ShieldCheck, Stethoscope } from 'lucide-react'
import { Fragment, useState } from 'react'
import { api } from '@/api/client'
import { CHECK_LABEL, isBlocking, type ConnectionInfo, type DoctorReport } from '@/api/connections'
import { atLeast } from '@/api/permissions'
import type { Workspace } from '@/api/types'
import { toastApiError } from '@/app/toast'
import { useRole } from '@/app/useRole'
import { DisabledReason } from '@/components/DisabledReason'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { CopyButton } from '../ticket/Header'
import { Mono, Pill, Section } from '../ticket/shared'
import { toast } from 'sonner'
import { CheckChip, checkTime, DEMO_RELOGIN, DemoChip, invalidateConnectionData, KIND_LABEL, PhaseChip, useConnections } from './connectionUi'

const TRIGGER_LABEL = { on_demand: 'run by hand', doctor: 'doctor', session_start: 'session start', claim: 'before a claim', relogin: 'after re-login' } as const
export const VIEWER_CHECK_REASON = 'Viewers cannot run checks.'

/** Run one connection's check (members and above); the answer replaces the connections list. */
export function useRunCheck(ws: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, trigger }: { name: string; trigger?: 'on_demand' | 'relogin' }) => api.runConnectionCheck(ws, name, trigger),
    onSuccess: (c, v) => {
      if (v.trigger === 'relogin' && c.last_check) toast.success(`${c.name}: ${CHECK_LABEL[c.last_check.status]}`, { description: DEMO_RELOGIN })
      return invalidateConnectionData(qc)
    },
    onError: (e) => toastApiError(e, 'Check failed to run'),
  })
}

function useNow(ws: string) {
  return useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws) }).data?.now
}

export function Connections({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const ws = workspace.id
  const role = useRole()
  const canRun = atLeast(role, 'member')
  const connections = useConnections(ws)
  const check = useRunCheck(ws)
  const qc = useQueryClient()
  const [report, setReport] = useState<DoctorReport | null>(null)
  const doctor = useMutation({
    mutationFn: () => api.runDoctor(ws),
    onSuccess: (r) => {
      setReport(r)
      void invalidateConnectionData(qc)
    },
    onError: (e) => toastApiError(e, 'The doctor could not run'),
  })
  const [open, setOpen] = useState<Set<string>>(new Set())
  const now = useNow(ws)
  const toggle = (n: string) => setOpen((s) => (s.has(n) ? new Set([...s].filter((x) => x !== n)) : new Set([...s, n])))
  if (!connections.data) return <Skeleton className="h-60 w-full" aria-busy="true" />
  const list = connections.data
  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">Connections</h2>
          <p className="mt-1 text-[13px] text-text-muted">
            Named tools and APIs, each bound to one identity. Skills reference them by name and never say how to authenticate. A failing auth or identity check blocks only the tickets that need that connection.
          </p>
        </div>
        <DisabledReason reason={canRun ? null : VIEWER_CHECK_REASON}>
          <Button size="sm" variant="outline" disabled={doctor.isPending} onClick={() => doctor.mutate()}>
            {doctor.isPending ? <Loader2 className="animate-spin" /> : <Stethoscope />}
            Run doctor
          </Button>
        </DisabledReason>
      </div>

      {report && <DoctorPanel report={report} onClose={() => setReport(null)} />}

      {list.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-[13px] text-text-muted">No connections in this workspace. They are configured in the workspace config.</p>
      ) : (
        <div className="rounded-lg border border-border bg-surface">
          <Table aria-label="Connections">
            <TableHeader>
              <TableRow>
                <TableHead>Name and identity</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Last check</TableHead>
                <TableHead className="w-44 text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.map((c) => {
                const last = c.last_check
                const running = check.isPending && check.variables?.name === c.name
                return (
                  <Fragment key={c.name}>
                    <TableRow data-testid={`connection-${c.name}`}>
                      <TableCell className="max-w-80 whitespace-normal">
                        <Mono className="text-text">{c.name}</Mono>
                        <p className="break-words text-[11px] text-text-muted">
                          {c.tool} · {c.target.label} <span className="text-text">{c.target.value}</span>
                        </p>
                      </TableCell>
                      <TableCell className="text-[12px] text-text-muted">{KIND_LABEL[c.kind]}</TableCell>
                      <TableCell className="whitespace-normal">
                        {last ? (
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                            <CheckChip status={last.status} />
                            <span className="text-[11px] tabular-nums text-text-muted">
                              {checkTime(last.at, now)} · {TRIGGER_LABEL[last.trigger]}
                            </span>
                            {last.trigger === 'relogin' && <DemoChip />}
                          </span>
                        ) : (
                          <CheckChip status="unknown" />
                        )}
                        {last?.status === 'wrong_identity' && (
                          <p className="mt-0.5 text-[11px] text-text-muted">
                            expected <span className="text-text">{last.expected}</span>, got <span className="text-danger">{last.actual}</span>
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <span className="inline-flex items-center gap-1">
                          <DisabledReason reason={canRun ? null : VIEWER_CHECK_REASON}>
                            <Button size="xs" variant="outline" disabled={running} onClick={() => check.mutate({ name: c.name })} aria-label={`Run check ${c.name}`}>
                              {running ? <Loader2 className="animate-spin" /> : <Play />}
                              Run check
                            </Button>
                          </DisabledReason>
                          <Button size="xs" variant="ghost" aria-expanded={open.has(c.name)} aria-label={`Details ${c.name}`} onClick={() => toggle(c.name)}>
                            Details
                            <ChevronDown className={cn('transition-transform', open.has(c.name) && 'rotate-180')} />
                          </Button>
                        </span>
                      </TableCell>
                    </TableRow>
                    {open.has(c.name) && (
                      <TableRow className="hover:bg-transparent">
                        <TableCell colSpan={4} className="whitespace-normal bg-bg">
                          <ConnectionDetails c={c} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="flex items-center gap-2 text-[12px] text-text-muted">
        Re-login prompts appear on Today for the owner; on the phone they come later. <PhaseChip phase="P4" />
      </p>

      <SecretsFilePanel workspace={workspace} canEdit={canEdit} />
    </div>
  )
}

/** Command, limits, the bounded and filtered output, the identity, the login hint and what uses the connection. */
export function ConnectionDetails({ c }: { c: ConnectionInfo }) {
  const last = c.last_check
  return (
    <div className="space-y-3 py-1 text-[13px]" data-testid={`details-${c.name}`}>
      <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1">
        <dt className="text-text-muted">Check</dt>
        <dd>
          <Mono>{c.check}</Mono>
        </dd>
        <dt className="text-text-muted">Limits</dt>
        <dd className="text-text-muted">
          timeout {last?.timeout_s ?? 10} s · stdin closed · output bounded to 12 lines
          {last && ` · took ${(last.duration_ms / 1000).toFixed(1)} s · exit ${last.exit_code ?? 'none (stopped)'}`}
        </dd>
        <dt className="text-text-muted">Runs as</dt>
        <dd>
          <Mono>{c.run_as}</Mono> <span className="text-text-muted">(the OS user that runs the agents)</span>
        </dd>
        {c.kind === 'cli_login' ? (
          <>
            <dt className="text-text-muted">Login</dt>
            <dd>
              <span className="text-text-muted">orch never copies or stores this tool&apos;s token; {c.tool} keeps its own. The login must exist for {c.run_as}.</span>
              {c.login_hint && (
                <span className="mt-1 flex items-center gap-1">
                  <code className="min-w-0 break-all rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[12px]">{c.login_hint}</code>
                  <CopyButton text={c.login_hint} label={`Copy the login command for ${c.name}`} />
                </span>
              )}
            </dd>
          </>
        ) : (
          <>
            <dt className="text-text-muted">Token</dt>
            <dd className="text-text-muted">
              Reads {c.env.map((n, i) => (
                <Fragment key={n}>
                  {i > 0 && ', '}
                  <Mono className="text-text">{n}</Mono>
                </Fragment>
              ))}{' '}
              from the secrets file, passed per invocation. Rotate by editing the file.
            </dd>
          </>
        )}
        {last?.status === 'wrong_identity' && (
          <>
            <dt className="text-text-muted">Identity</dt>
            <dd>
              expected <Mono className="text-text">{last.expected}</Mono>, the check reported <Mono className="text-danger">{last.actual}</Mono>
            </dd>
          </>
        )}
        <dt className="text-text-muted">Used by</dt>
        <dd className="flex flex-wrap items-center gap-1.5">
          {c.skills.length === 0 && <span className="text-text-faint">no skill</span>}
          {c.skills.map((s) => (
            <Pill key={s}>{s}</Pill>
          ))}
          {c.tickets.map((k) => (
            <Link key={k} to="/ticket/$key" params={{ key: k }} className="font-mono text-[12px] text-brand hover:underline">
              {k}
              {last && isBlocking(last.status) && ' (blocked)'}
            </Link>
          ))}
        </dd>
      </dl>
      {last && (
        <div>
          {last.trigger === 'relogin' && (
            <p className="mb-1 flex items-center gap-2 text-[12px] text-text-muted">
              <DemoChip />
              {DEMO_RELOGIN}
            </p>
          )}
          <p className="mb-1 text-[12px] text-text-muted">
            Output ({CHECK_LABEL[last.status]}, filtered for secret values{last.truncated ? ', cut' : ''})
          </p>
          <pre aria-label={`Check output ${c.name}`} className="max-h-48 overflow-auto rounded-md border border-border bg-surface p-2 font-mono text-[12px] leading-snug text-text">
            {last.output.join('\n')}
          </pre>
        </div>
      )}
    </div>
  )
}

function DoctorPanel({ report, onClose }: { report: DoctorReport; onClose: () => void }) {
  const failing = report.checks.filter((c) => c.status !== 'ok').length
  return (
    <Section
      title={<span className="flex items-center gap-2"><Stethoscope className="size-4 text-text-muted" aria-hidden />orch doctor · {checkTime(report.at)}</span>}
      aside={
        <Button size="xs" variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="space-y-3 text-[13px]" data-testid="doctor-report">
        <p className="text-text-muted">
          {report.checks.length} checks · {failing === 0 ? 'all ok' : `${failing} not ok`} · {report.unknown_skills.length} {report.unknown_skills.length === 1 ? 'skill' : 'skills'} with unknown or invalid needs · secrets file{' '}
          {report.secrets_permissions_ok ? 'permissions ok' : 'permissions wrong'}
        </p>
        <ul className="space-y-1">
          {report.checks.map((c) => (
            <li key={c.name} className="flex items-center gap-2">
              <CheckChip status={c.status} className="w-28 justify-center" />
              <Mono className="w-32 shrink-0 text-text">{c.name}</Mono>
              <span className="min-w-0 truncate text-[12px] text-text-muted" title={c.summary}>
                {c.summary}
              </span>
            </li>
          ))}
        </ul>
        {report.unknown_skills.length > 0 && (
          <div>
            <p className="font-medium">Warning: skills without a valid orch.skill.json</p>
            <ul className="mt-1 list-disc pl-5 text-text-muted">
              {report.unknown_skills.map((s) => (
                <li key={s.name}>
                  <Mono className="text-text">{s.name}</Mono> · {s.needs === 'invalid' ? 'invalid sidecar' : 'no sidecar (unknown needs)'} · {s.path}
                </li>
              ))}
            </ul>
          </div>
        )}
        {report.ungranted.length > 0 && (
          <div>
            <p className="font-medium">References waiting for an owner&apos;s grant</p>
            <ul className="mt-1 list-disc pl-5 text-text-muted">
              {report.ungranted.map((u) => (
                <li key={u.skill}>
                  <Mono className="text-text">{u.skill}</Mono>: {u.refs.join(', ')}
                </li>
              ))}
            </ul>
          </div>
        )}
        {report.secrets_problems.length > 0 && (
          <div>
            <p className="font-medium">Secrets file lines ignored</p>
            <ul className="mt-1 list-disc pl-5 text-text-muted">
              {report.secrets_problems.map((p) => (
                <li key={p.line}>
                  Line {p.line}: {p.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
        {report.stale_sessions.length > 0 && <StaleSessions sessions={report.stale_sessions} />}
      </div>
    </Section>
  )
}

function StaleSessions({ sessions }: { sessions: DoctorReport['stale_sessions'] }) {
  return (
    <div>
      <p className="font-medium">Sessions started before the secrets file&apos;s last change</p>
      <p className="text-[12px] text-text-muted">They keep the old values until restarted: restart to pick up the new value.</p>
      <ul className="mt-1 space-y-0.5">
        {sessions.map((s) => (
          <li key={`${s.session}:${s.ticket}`} className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
            <Mono className="text-text">{s.session}</Mono>
            <span>{s.agent}</span>
            <Link to="/ticket/$key" params={{ key: s.ticket }} className="font-mono text-[12px] text-brand hover:underline">
              {s.ticket}
            </Link>
            <span className="text-[12px] text-text-muted">started {checkTime(s.started)}</span>
            <Link to="/agents" className="text-[12px] text-brand hover:underline">
              Restart on Agents
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The secrets file: where it is, its permissions and the names in it. Never a value. */
function SecretsFilePanel({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const role = useRole()
  const allowed = atLeast(role, 'maintainer')
  const f = useQuery({ queryKey: ['secrets', workspace.id], queryFn: () => api.getSecretsFile(workspace.id), enabled: allowed, retry: false })
  const title = (
    <span className="flex items-center gap-2">
      <FileLock2 className="size-4 text-text-muted" aria-hidden />
      Secrets file (API tokens)
    </span>
  )
  if (!allowed) {
    return (
      <Section title={title}>
        <p className="text-[13px] text-text-muted">Only owners and maintainers see the secrets file (names only).</p>
      </Section>
    )
  }
  if (!f.data) return <Skeleton className="h-40 w-full" />
  const s = f.data
  return (
    <Section title={title}>
      <div className="space-y-3 text-[13px]" data-testid="secrets-file">
        <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1">
          <dt className="text-text-muted">Path</dt>
          <dd>
            <Mono className="break-all text-text">{s.path}</Mono>
            <p className="text-[11px] text-text-faint">{s.template}, outside every repo</p>
          </dd>
          <dt className="text-text-muted">Permissions</dt>
          <dd className="flex flex-wrap items-center gap-2">
            {s.permissions_ok ? <ShieldCheck className="size-3.5 text-success" aria-hidden /> : <ShieldAlert className="size-3.5 text-danger" aria-hidden />}
            <span>
              directory <Mono>{s.dir_mode}</Mono>, file <Mono>{s.file_mode}</Mono>, owner <Mono>{s.owner_user}</Mono>
            </span>
            <Pill tone={s.permissions_ok ? 'success' : 'danger'}>{s.permissions_ok ? 'ok' : 'wrong'}</Pill>
          </dd>
          {s.exists && (
            <>
              <dt className="text-text-muted">Changed</dt>
              <dd>{checkTime(s.modified_at)}</dd>
            </>
          )}
          <dt className="text-text-muted">Format</dt>
          <dd className="text-text-muted">
            <Mono>NAME=value</Mono> lines and <Mono>#</Mono> comments. orch parses it: no sourcing, no expansion, no command substitution. Values are never shown here, and output of checks and orch-run tools is filtered for them.
          </dd>
        </dl>
        {!s.exists ? (
          <p className="text-text-muted">No secrets file for this workspace yet. Create it on the host with the permissions above when a connection needs an API token.</p>
        ) : (
          <Table aria-label="Names in the secrets file">
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">Line</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Used by</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.names.map((n) => (
                <TableRow key={n.name}>
                  <TableCell className="tabular-nums text-text-muted">{n.line}</TableCell>
                  <TableCell>
                    <Mono className="text-text">{n.name}</Mono>
                    <span className="ml-2 font-mono text-[12px] text-text-faint" aria-label="value hidden">
                      = ••••
                    </span>
                  </TableCell>
                  <TableCell className="whitespace-normal text-[12px] text-text-muted">
                    {n.skills.length + n.connections.length === 0 ? 'not used by any skill or connection' : [...n.connections.map((x) => `connection ${x}`), ...n.skills.map((x) => `skill ${x}`)].join(' · ')}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {s.problems.length > 0 && (
          <ul className="space-y-0.5 text-[12px]">
            {s.problems.map((p) => (
              <li key={p.line} className="text-warning">
                Line {p.line} ignored: {p.reason}
              </li>
            ))}
          </ul>
        )}
        {s.stale_sessions.length > 0 && <StaleSessions sessions={s.stale_sessions} />}
        <p className="text-[12px] text-text-muted">
          Rotate a token by editing the file on the host{canEdit ? '' : ' (owners)'}. Running sessions keep the old value until restarted; the doctor lists them.
        </p>
      </div>
    </Section>
  )
}
