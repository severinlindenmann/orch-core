// Shared bits of the Skills and Connections tabs, the ticket rail and Today (D55–D57).
import { useQuery, type QueryClient } from '@tanstack/react-query'
import { CircleCheck, CircleHelp, CloudOff, KeyRound, UserX } from 'lucide-react'
import { api } from '@/api/client'
import { CHECK_LABEL, type CheckStatus } from '@/api/connections'
import { cn } from '@/lib/utils'
import { fmtWhen } from '@/lib/time'

const TONE: Record<CheckStatus, string> = {
  ok: 'border-success/40 bg-success-soft text-success',
  auth_expired: 'border-danger/40 bg-danger-soft text-danger',
  wrong_identity: 'border-danger/40 bg-danger-soft text-danger',
  service_down: 'border-warning/40 bg-warning-soft text-warning',
  unknown: 'border-border text-text-muted',
}
const ICON: Record<CheckStatus, typeof CircleCheck> = { ok: CircleCheck, auth_expired: KeyRound, wrong_identity: UserX, service_down: CloudOff, unknown: CircleHelp }

/** A check result as a chip: ok, auth expired, wrong identity, service down, unknown. Never orange. */
export function CheckChip({ status, className }: { status: CheckStatus; className?: string }) {
  const Icon = ICON[status]
  return (
    <span data-testid="check-status" className={cn('inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-full border px-2 text-[11px] font-medium', TONE[status], className)}>
      <Icon className="size-3" aria-hidden />
      {CHECK_LABEL[status]}
    </span>
  )
}

/** A part the spec puts in a later phase: "P6 · Preview". Neutral outline, like core's Preview chip. */
export function PhaseChip({ phase, className }: { phase: string; className?: string }) {
  return (
    <span className={cn('inline-flex shrink-0 select-none items-center rounded-full border border-border px-1.5 py-px text-[10px] font-medium leading-4 text-text-muted', className)}>
      {phase} · Preview
    </span>
  )
}

/** The mock's re-login path: a check from the re-login item assumes the owner logged in again. Said on screen. */
export const DEMO_RELOGIN = 'Demo: this check assumes you logged in again.'
export function DemoChip({ className }: { className?: string }) {
  return (
    <span title={DEMO_RELOGIN} className={cn('inline-flex shrink-0 select-none items-center rounded-full border border-info/40 bg-info-soft px-1.5 py-px text-[10px] font-medium leading-4 text-info', className)}>
      Demo
    </span>
  )
}

/** What a check, the doctor or a grant can change: connections, skills, the secrets file, tickets (needs) and Today. */
export function invalidateConnectionData(qc: QueryClient) {
  return Promise.all(['connections', 'skills', 'secrets', 'ticket', 'today', 'workspaces'].map((k) => qc.invalidateQueries({ queryKey: [k] })))
}

export const KIND_LABEL = { cli_login: 'CLI login', api_token: 'API token' } as const

export const useConnections = (ws: string | undefined) => useQuery({ queryKey: ['connections', ws], queryFn: () => api.getConnections(ws!), enabled: !!ws })
export const useSkills = (ws: string | undefined) => useQuery({ queryKey: ['skills', ws], queryFn: () => api.getSkills(ws!), enabled: !!ws })

/** When a check ran, in the one format (src/lib/time.ts): "5 min ago", or "2 Oct 10:47" when older than a week. */
export function checkTime(iso: string, now?: string): string {
  return fmtWhen(iso, now)
}
