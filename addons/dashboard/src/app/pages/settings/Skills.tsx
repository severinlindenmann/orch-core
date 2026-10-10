// Settings › Skills (D55): skills by scope, each with its sidecar status and what it may use. Adding a connection
// reference or an env name to a skill is a credential grant, signed by the owner in core's dialog, separate from
// edits to the skill's prose (those are ordinary changes in the workspace repo).
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { FileText, KeyRound, Plug, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import { ENV_RE, SCOPE_LABEL, SKILL_SCOPES, type CheckStatus, type SkillInfo, type SkillScope } from '@/api/connections'
import type { Workspace } from '@/api/types'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { Mono, Pill, Section } from '../ticket/shared'
import { CheckChip, checkTime, KIND_LABEL, PhaseChip, useConnections, useSkills } from './connectionUi'
import { fmtWhen } from '@/lib/time'

export const GRANT_OWNER_ONLY = 'Only owners grant credentials to skills.'

/** The sidecar status: ok, or the doctor's "unknown needs" warning. */
export function SidecarStatus({ skill }: { skill: SkillInfo }) {
  if (skill.needs === 'declared') return <Pill tone="success">orch.skill.json</Pill>
  if (skill.needs === 'invalid')
    return (
      <Pill tone="danger">
        <TriangleAlert />
        invalid sidecar
      </Pill>
    )
  return (
    <Pill tone="warning">
      <TriangleAlert />
      unknown needs
    </Pill>
  )
}

/** Connection references and env names of a skill; one not granted yet says so. */
function Refs({ skill }: { skill: SkillInfo }) {
  const refs = [...skill.connections.map((r) => ({ ...r, env: false })), ...skill.env.map((r) => ({ ...r, env: true }))]
  if (skill.needs === 'invalid') return <span className="text-text-faint">nothing until orch.skill.json validates</span>
  if (skill.needs !== 'declared') return <span className="text-text-faint">not declared</span>
  if (refs.length === 0) return <span className="text-text-faint">none</span>
  return (
    <span className="flex flex-wrap gap-1">
      {refs.map((r) => (
        <span key={`${r.env}:${r.name}`} className={cn('inline-flex items-center gap-1 rounded border px-1.5 py-px font-mono text-[11px]', r.granted ? 'border-border text-text' : 'border-warning/40 bg-warning-soft text-warning')}>
          {r.env ? <KeyRound className="size-3" aria-hidden /> : <Plug className="size-3" aria-hidden />}
          {r.name}
          {!r.granted && <span className="font-sans">· needs grant</span>}
        </span>
      ))}
    </span>
  )
}

const SCOPE_NOTE: Record<SkillScope, string> = {
  built_in: 'From orch-core and installed addons; they change with a release.',
  workspace: 'In the workspace repo under skills/. Edit the prose freely; what a skill may use needs your grant.',
  org: 'Signed by the org, sealed to members via the relay and pinned by hash. A new or changed org skill needs one owner approval per workspace before agents use it.',
}

export function Skills({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const skills = useSkills(workspace.id)
  const [open, setOpen] = useState<string | null>(null)
  if (!skills.data) return <Skeleton className="h-60 w-full" aria-busy="true" />
  const current = skills.data.find((s) => s.name === open)
  const unknown = skills.data.filter((s) => s.needs !== 'declared').length
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Skills</h2>
        <p className="mt-1 text-[13px] text-text-muted">
          Instructions agents load, in the Claude Code SKILL.md format. What a skill may use (connections and env names) lives in its orch.skill.json sidecar.
          {unknown > 0 && ` ${unknown} ${unknown === 1 ? 'skill has' : 'skills have'} no valid sidecar: their needs are unknown and the doctor warns.`}
        </p>
      </div>
      {SKILL_SCOPES.map((scope) => {
        const list = skills.data.filter((s) => s.scope === scope)
        return (
          <Section
            key={scope}
            title={
              <span className="flex items-center gap-2">
                {SCOPE_LABEL[scope]}
                <span className="font-normal text-text-muted">· {list.length}</span>
                {scope === 'org' && <PhaseChip phase="P6" />}
              </span>
            }
          >
            <p className="mb-2 text-[12px] text-text-muted">{SCOPE_NOTE[scope]}</p>
            {list.length === 0 ? (
              <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-[13px] text-text-muted">
                {scope === 'org' ? 'Org skills come with the relay in P6. Nothing to show yet.' : 'No skills here.'}
              </p>
            ) : (
              <Table aria-label={`${SCOPE_LABEL[scope]} skills`}>
                <TableHeader>
                  <TableRow>
                    <TableHead>Skill</TableHead>
                    <TableHead className="w-20">Version</TableHead>
                    <TableHead className="w-36">Sidecar</TableHead>
                    <TableHead>Uses</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {list.map((s) => (
                    <TableRow key={s.name}>
                      <TableCell className="max-w-80 whitespace-normal">
                        <button type="button" onClick={() => setOpen(s.name)} className="font-mono text-[12px] text-brand hover:underline" aria-label={`Open skill ${s.name}`}>
                          {s.name}
                        </button>
                        <p className="line-clamp-2 text-[12px] text-text-muted">{s.description}</p>
                      </TableCell>
                      <TableCell className="font-mono text-[12px] tabular-nums text-text-muted">{s.version ?? '–'}</TableCell>
                      <TableCell>
                        <SidecarStatus skill={s} />
                      </TableCell>
                      <TableCell className="whitespace-normal">
                        <Refs skill={s} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Section>
        )
      })}
      <Sheet open={!!current} onOpenChange={(o) => !o && setOpen(null)}>
        <SheetContent side="right" className="w-[600px] max-w-[92vw] gap-0 border-border bg-surface p-0 sm:max-w-[600px]">
          {current && <SkillDetail skill={current} workspace={workspace} canEdit={canEdit} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}

/** SKILL.md without its frontmatter (name and description are shown above it). */
const bodyOf = (md: string) => md.replace(/^---\n[\s\S]*?\n---\n/, '').trim()

interface GrantAsk {
  connections: string[]
  env: string[]
}

function SkillDetail({ skill, workspace, canEdit }: { skill: SkillInfo; workspace: Workspace; canEdit: boolean }) {
  const connections = useConnections(workspace.id)
  // Names in the secrets file help the owner pick an env name (owners and maintainers only; names, never values).
  const secrets = useQuery({ queryKey: ['secrets', workspace.id], queryFn: () => api.getSecretsFile(workspace.id), enabled: canEdit, retry: false })
  const signed = useSignedAction()
  const [ask, setAsk] = useState<GrantAsk | null>(null)
  const [conn, setConn] = useState('')
  const [env, setEnv] = useState('')
  const workspaceSkill = skill.scope === 'workspace'
  const reason = !canEdit
    ? GRANT_OWNER_ONLY
    : !workspaceSkill
      ? 'Built-in skills change with a release; their needs come with it.'
      : skill.needs === 'invalid'
        ? 'Fix orch.skill.json in the workspace repo before granting.'
        : null
  const pendingConns = skill.connections.filter((r) => !r.granted).map((r) => r.name)
  const pendingEnv = skill.env.filter((r) => !r.granted).map((r) => r.name)
  const referenced = new Set(skill.connections.map((r) => r.name))
  const addable = (connections.data ?? []).filter((c) => !referenced.has(c.name))
  const envOk = env === '' || ENV_RE.test(env)
  const describeConn = (name: string) => {
    const c = connections.data?.find((x) => x.name === name)
    return c ? `Connection ${name} (${KIND_LABEL[c.kind]}, ${c.tool}, ${c.target.label} ${c.target.value})` : `Connection ${name}`
  }
  const inFile = (name: string) => secrets.data?.names.some((n) => n.name === name)
  const covers = (a: GrantAsk) => [
    `Skill ${skill.name} (workspace, ${skill.path})`,
    ...a.connections.map(describeConn),
    ...a.env.map((n) => `${n}: the agent session gets this value in its environment — the agent can read it${secrets.data ? (inFile(n) ? '' : ' (not in the secrets file yet)') : ''}`),
    ...(a.connections.length ? ['When orch runs a connection check or a tool itself, it passes values per invocation only'] : []),
    'Only sessions on tickets that use this skill get them',
    'Edits to the skill\'s prose need no signature',
  ]

  return (
    <>
      <SheetHeader className="border-b border-border">
        <SheetTitle className="flex items-center gap-2">
          <FileText className="size-4 text-text-muted" aria-hidden />
          <span className="font-mono">{skill.name}</span>
          <Pill>{SCOPE_LABEL[skill.scope]}</Pill>
          {skill.version && <Pill>v{skill.version}</Pill>}
          <SidecarStatus skill={skill} />
        </SheetTitle>
        <SheetDescription>{skill.description}</SheetDescription>
      </SheetHeader>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        <dl className="grid grid-cols-[96px_1fr] gap-x-3 gap-y-1 text-[13px]">
          <dt className="text-text-muted">Path</dt>
          <dd>
            <Mono>{skill.path}</Mono>
          </dd>
          <dt className="text-text-muted">Source</dt>
          <dd>{skill.source}</dd>
          <dt className="text-text-muted">Used by</dt>
          <dd className="flex flex-wrap gap-1.5">
            {skill.tickets.length === 0 ? (
              <span className="text-text-faint">no ticket you can see</span>
            ) : (
              skill.tickets.map((k) => (
                <Link key={k} to="/ticket/$key" params={{ key: k }} className="font-mono text-[12px] text-brand hover:underline">
                  {k}
                </Link>
              ))
            )}
          </dd>
        </dl>

        <Section title="What it may use">
          {skill.needs === 'invalid' ? (
            <p className="flex items-start gap-2 text-[13px] text-text-muted" data-testid="sidecar-problem">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden />
              orch.skill.json does not validate ({skill.sidecar_problem}). Agents get no connection or env from this skill until the file is fixed in the workspace repo; grants wait for that.
            </p>
          ) : skill.needs !== 'declared' ? (
            <p className="flex items-start gap-2 text-[13px] text-text-muted">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
              No orch.skill.json: the needs are unknown, not "none". Agents get no connection or env from this skill, and the doctor warns until a sidecar is added (a grant below writes one).
            </p>
          ) : (
            <ul className="space-y-1 text-[13px]">
              {skill.connections.length + skill.env.length === 0 && <li className="text-text-muted">Nothing: no connection, no env name.</li>}
              {skill.connections.map((r) => (
                <li key={`c:${r.name}`} className="flex items-center gap-2">
                  <Plug className="size-3.5 text-text-muted" aria-hidden />
                  <Mono>{r.name}</Mono>
                  <span className="text-text-muted">connection</span>
                  {r.granted ? <Pill tone="success">granted</Pill> : <Pill tone="warning">waits for your grant</Pill>}
                  <ConnectionCheck name={r.name} connections={connections.data} />
                </li>
              ))}
              {skill.env.map((r) => (
                <li key={`e:${r.name}`} className="flex items-center gap-2">
                  <KeyRound className="size-3.5 text-text-muted" aria-hidden />
                  <Mono>{r.name}</Mono>
                  <span className="text-text-muted">env</span>
                  {r.granted ? <Pill tone="success">granted</Pill> : <Pill tone="warning">waits for your grant</Pill>}
                  {!r.in_secrets_file && <span className="text-[12px] text-text-faint">not in the secrets file</span>}
                </li>
              ))}
            </ul>
          )}
          {skill.pending_note && pendingConns.length + pendingEnv.length > 0 && <p className="mt-2 text-[12px] text-text-muted">{skill.pending_note}</p>}

          <div className="mt-3 space-y-2 border-t border-border pt-3">
            <p className="text-[12px] text-text-muted">Adding a connection or an env name is a credential grant: you sign it with Touch ID. Prose edits to SKILL.md are separate and need no signature.</p>
            {reason ? (
              <p className="text-[12px] text-text-muted" data-testid="grant-reason">
                {reason}
              </p>
            ) : (
              <>
                {pendingConns.length + pendingEnv.length > 0 && (
                  <Button size="sm" onClick={() => setAsk({ connections: pendingConns, env: pendingEnv })}>
                    Grant {[...pendingConns, ...pendingEnv].join(', ')}…
                  </Button>
                )}
                <div className="flex flex-wrap items-end gap-2">
                  <div className="space-y-1">
                    <Label htmlFor="grant-conn" className="text-[12px]">
                      Connection
                    </Label>
                    <select id="grant-conn" value={conn} onChange={(e) => setConn(e.target.value)} className="h-8 rounded-md border border-border bg-bg px-2 text-[13px]">
                      <option value="">none</option>
                      {addable.map((c) => (
                        <option key={c.name} value={c.name}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="grant-env" className="text-[12px]">
                      Env name
                    </Label>
                    <Input id="grant-env" list="grant-env-names" value={env} onChange={(e) => setEnv(e.target.value.trim())} placeholder="DATABRICKS_TOKEN" aria-invalid={!envOk} className="h-8 w-48 font-mono text-[12px]" />
                    <datalist id="grant-env-names">
                      {(secrets.data?.names ?? []).map((n) => (
                        <option key={n.name} value={n.name} />
                      ))}
                    </datalist>
                  </div>
                  <Button size="sm" variant="outline" disabled={(!conn && !env) || !envOk} onClick={() => setAsk({ connections: conn ? [conn] : [], env: env ? [env] : [] })}>
                    Add…
                  </Button>
                </div>
                {!envOk && <p className="text-[12px] text-danger">Upper case letters, digits and _, starting with a letter.</p>}
              </>
            )}
          </div>
        </Section>

        <Section title={`Grant history · ${skill.grants.length}`}>
          {skill.grants.length === 0 ? (
            <p className="text-[13px] text-text-muted">{skill.scope === 'built_in' ? 'Built in: what it uses comes with the release (an addon skill with the addon\'s grant).' : 'No grants yet.'}</p>
          ) : (
            <ul className="space-y-1 text-[13px]">
              {[...skill.grants].reverse().map((g, i) => (
                <li key={`${g.at}:${i}`} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="tabular-nums text-text-muted">{checkTime(g.at)}</span>
                  <span>{workspace.members.find((m) => m.person === g.by)?.name ?? g.by}</span>
                  <span className="text-text-muted">granted</span>
                  <span className="font-mono text-[12px]">{[...g.connections, ...g.env].join(', ')}</span>
                  <span className="text-[11px] text-text-faint">Touch ID</span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="SKILL.md">
          <SafeMarkdown text={bodyOf(skill.skill_md)} />
        </Section>
        <Section title="orch.skill.json">
          {skill.sidecar ? (
            <pre className="overflow-x-auto rounded-md bg-bg p-2 font-mono text-[12px] leading-snug text-text">{skill.sidecar}</pre>
          ) : (
            <p className="text-[13px] text-text-muted">None next to SKILL.md.</p>
          )}
        </Section>
      </div>
      {ask && (
        <SignPrompt
          title={`Grant credentials to ${skill.name}`}
          description="A credential grant, signed with your own key. Only core shows this prompt; an agent or an addon cannot sign it."
          covers={covers(ask)}
          confirmLabel="Sign grant"
          onClose={() => setAsk(null)}
          onSign={() => {
            const a = ask
            setAsk(null)
            setConn('')
            setEnv('')
            void signed(`Grant to ${skill.name}`, () => api.grantSkillCredentials(workspace.id, skill.name, { ...a, confirmed: true }))
          }}
        />
      )}
    </>
  )
}

/**
 * The connection's own state next to the grant: "granted" says the skill may use it, not that it works. The last check
 * is the one the Connections tab shows (same data), so the two never disagree.
 */
function ConnectionCheck({ name, connections }: { name: string; connections?: { name: string; last_check: { status: CheckStatus; at: string } | null }[] }) {
  if (!connections) return null
  const c = connections.find((x) => x.name === name)
  if (!c) return <span className="text-[12px] text-warning">not set up in Connections</span>
  if (!c.last_check) return <span className="text-[12px] text-text-faint">never checked</span>
  return (
    <span className="inline-flex items-center gap-1.5 text-[12px] text-text-faint">
      <CheckChip status={c.last_check.status} />
      last check {fmtWhen(c.last_check.at)}
    </span>
  )
}
