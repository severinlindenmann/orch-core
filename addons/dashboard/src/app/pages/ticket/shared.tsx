import { useQuery } from '@tanstack/react-query'
import { Bot, Cpu, User } from 'lucide-react'
import { type ReactNode } from 'react'
import { api } from '@/api/client'
import { roleOf } from '@/api/permissions'
import { workspaceOfTicket } from '@/api/workspaces'
import type { Member, Priority, Role, Status, TicketDocument, TicketLanding } from '@/api/types'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { cn } from '@/lib/utils'
import { usePageWidth } from '../../pageWidth'

// ------------------------------------------------------------------ viewer + people

export interface Viewer {
  person: string
  role: Role | undefined
  members: Member[]
  name: (id: string | null | undefined) => string
  /** False while the workspace and viewer are still loading (names would flash as ids). */
  ready: boolean
}

/** The viewer's role in the ticket's home workspace (`workspaceOfTicket`) and a name lookup for person ids. */
export function useViewer(ticketKey: string): Viewer {
  const ws = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const workspace = workspaceOfTicket(ticketKey, ws.data ?? [])
  const members = workspace?.members ?? []
  const person = me.data?.person ?? ''
  return {
    person,
    role: roleOf(workspace, person),
    members,
    name: (id) => displayName(members, id),
    ready: !ws.isPending && !me.isPending,
  }
}

export function displayName(members: Member[], id: string | null | undefined): string {
  if (!id) return 'nobody'
  if (id.startsWith('agent:')) return agentName(id.slice(6))
  const m = members.find((x) => x.person === id)
  if (m) return m.name
  return agentName(id)
}

export function agentName(id: string): string {
  if (id === 'claude-code') return 'Claude Code'
  if (id === 'codex') return 'Codex'
  return id
}

const AVATAR_TONES = ['bg-brand-soft text-brand', 'bg-info-soft text-info', 'bg-warning-soft text-warning', 'bg-success-soft text-success']

export function PersonAvatar({ id, name, size = 'sm' }: { id: string; name: string; size?: 'sm' | 'default' }) {
  const tone = AVATAR_TONES[[...id].reduce((a, c) => a + c.charCodeAt(0), 0) % AVATAR_TONES.length]
  return (
    <Avatar size={size} aria-hidden>
      <AvatarFallback className={cn('text-[11px] font-semibold', tone)}>{name.slice(0, 1).toUpperCase()}</AvatarFallback>
    </Avatar>
  )
}

export function PersonChip({ id, viewer, role }: { id: string; viewer: Viewer; role?: string }) {
  const name = viewer.name(id)
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px]">
      <PersonAvatar id={id} name={name} />
      <span className="text-text">{name}</span>
      {role && <span className="text-[11px] text-text-faint">{role}</span>}
    </span>
  )
}

// ------------------------------------------------------------------ time

// One formatter for the whole dashboard (src/lib/time.ts); these names stay for the ticket page's callers.
export { fmtDateTime as fmtTime, fmtDay, fmtClock, fmtExact, fmtWhen as ago } from '@/lib/time'

/** Viewport at least `min` px wide (the ticket rail sits next to the content from 1280 px; below it is a sheet). */
/** Room for the wide layout? Reads the page's layout width (the window minus a right-hand terminal dock). */
export function useWideLayout(min = 1280): boolean {
  return usePageWidth() >= min
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`
  const m = Math.floor(s / 60)
  return `${m} min ${Math.round(s % 60)} s`
}

export function fmtBytes(n: number): string {
  if (n === 0) return '0 B'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

// ------------------------------------------------------------------ chips

export const STATUS_LABEL: Record<Status, string> = {
  backlog: 'Backlog',
  open: 'Open',
  'in-progress': 'In progress',
  waiting: 'Waiting',
  testing: 'Testing',
  done: 'Done',
}

const STATUS_TONE: Record<Status, string> = {
  backlog: 'border-border-strong text-text-muted',
  open: 'border-info/40 bg-info-soft text-info',
  'in-progress': 'border-brand/40 bg-brand-soft text-brand',
  waiting: 'border-warning/40 bg-warning-soft text-warning',
  testing: 'border-text-muted/50 bg-surface-3 text-text',
  done: 'border-success/40 bg-success-soft text-success',
}

/** A done ticket that is still landing (queued, checked or failed) is not shown as Done (core's `landing`). */
export function statusLabel(status: Status, landing?: TicketLanding): string {
  if (status === 'done' && landing) return landing.state === 'failed' ? 'Landing failed' : 'Landing'
  return STATUS_LABEL[status]
}

function landingTitle(status: Status, landing?: TicketLanding): string | undefined {
  if (status !== 'done' || !landing) return undefined
  if (landing.state === 'queued') return 'Approved and waiting to land on the target branch. Done once it merged.'
  const why = landing.reason === 'conflict' ? 'a conflict' : landing.reason === 'timeout' ? 'checks that timed out' : 'red checks'
  return `Approved, but landing attempt #${landing.attempt ?? '?'} failed with ${why}. Not done until it lands.`
}

export function StatusChip({ status, landing }: { status: Status; landing?: TicketLanding }) {
  const tone = status === 'done' && landing ? (landing.state === 'failed' ? 'border-danger/40 bg-danger-soft text-danger' : 'border-info/40 bg-info-soft text-info') : STATUS_TONE[status]
  return (
    <span className={cn('inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-xs font-medium', tone)} data-testid="status-chip" title={landingTitle(status, landing)}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {statusLabel(status, landing)}
    </span>
  )
}

const PRIORITY_TONE: Record<Priority, string> = {
  low: 'text-text-muted',
  medium: 'text-info',
  high: 'text-warning',
  urgent: 'text-danger',
}

export function PriorityLabel({ priority }: { priority: Priority }) {
  return <span className={cn('text-[13px] font-medium capitalize', PRIORITY_TONE[priority])}>{priority}</span>
}

export function Pill({ children, tone = 'neutral', className }: { children: ReactNode; tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'brand'; className?: string }) {
  const tones = {
    neutral: 'border-border text-text-muted',
    success: 'border-success/40 bg-success-soft text-success',
    warning: 'border-warning/40 bg-warning-soft text-warning',
    danger: 'border-danger/40 bg-danger-soft text-danger',
    info: 'border-info/40 bg-info-soft text-info',
    brand: 'border-brand/40 bg-brand-soft text-brand',
  }
  return <span className={cn('inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-full border px-2 text-[11px] font-medium [&>svg]:size-3', tones[tone], className)}>{children}</span>
}

export function ActorIcon({ kind, className }: { kind: 'person' | 'agent' | 'host'; className?: string }) {
  const Icon = kind === 'person' ? User : kind === 'agent' ? Bot : Cpu
  return <Icon className={cn('size-3.5', className)} aria-label={kind} />
}

export function Section({ title, aside, children, className }: { title: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn('rounded-lg border border-border bg-surface', className)}>
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h3 className="flex-1 text-[13px] font-semibold text-text">{title}</h3>
        {aside}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('font-mono text-[12px]', className)}>{children}</span>
}

export function shortHash(h: string, n = 8): string {
  return h.replace(/^sha256:/, '').replace(/…$/, '').slice(0, n)
}

export type TabId = 'overview' | 'acceptance' | 'changes' | 'questions' | 'artifacts' | 'history' | 'raw'

export interface Jump {
  tab: TabId
  id?: string
}

/** Everything a tab needs to render and to change the ticket. */
export interface TabProps {
  ticket: TicketDocument
  viewer: Viewer
  jump: (j: Jump) => void
  /** Opens the signing dialog for a human action. */
  sign: (a: HumanAction) => void
}

export type HumanAction =
  | { kind: 'approve'; gate: 'requirements' | 'plan' | 'code' }
  | { kind: 'request_changes'; gate: 'requirements' | 'plan' | 'verify' | 'code' }
  | { kind: 'verdict' }
  | { kind: 'answer'; question: string; option?: string; text?: string }
