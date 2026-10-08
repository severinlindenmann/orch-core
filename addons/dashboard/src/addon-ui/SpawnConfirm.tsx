import { useQuery } from '@tanstack/react-query'
import { Bot, TriangleAlert } from 'lucide-react'
import { api } from '@/api/client'
import { activeGrantOf } from '@/api/grants'
import { can } from '@/api/permissions'
import type { LaunchPreview } from '@/api/types'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { canSpawnAgent } from './capabilities'
import { useAddons } from './slots'

/** Hours of the grant a person signs here when they have none. */
const GRANT_HOURS = 8
const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`

/**
 * Core's confirmation before an addon's `spawn_agent` action runs (never an addon node). It shows what will start —
 * ticket, mode, harness, where, model line and the exact command — and the grant it runs under. With an active grant
 * it is a plain confirmation; without one, an owner or maintainer signs a grant here first (SignPrompt, Touch ID),
 * a member is told who can issue one. `onStart` posts the action with core's `confirmed` flag.
 */
export function SpawnConfirm({ addon, ticketKey, onStart, onClose }: { addon: string; ticketKey?: string; onStart: () => void; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const signed = useSignedAction()
  const { data: addons } = useAddons()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  const state = useQuery({ queryKey: ['addon-state', ws, addon], queryFn: () => api.getAddonState(ws!, addon), enabled: !!ws, retry: false })

  const allowed = canSpawnAgent(addons?.find((a) => a.name === addon), workspace?.addons[addon])
  const previews = (state.data?.previews ?? {}) as Record<string, LaunchPreview>
  const selected = state.data?.selected
  const key = ticketKey ?? (typeof selected === 'string' ? selected : undefined)
  const p = key && Object.hasOwn(previews, key) ? previews[key] : undefined

  if (!ws || !me.data || !today.data || !grants.data || !state.data) {
    return (
      <Dialog open onOpenChange={(o) => !o && onClose()}>
        <DialogContent className="max-w-lg border-border bg-surface" aria-busy="true">
          <DialogHeader>
            <DialogTitle>Start agent</DialogTitle>
            <DialogDescription>Checking your grant…</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    )
  }
  if (!p || !allowed) {
    return (
      <Dialog open onOpenChange={(o) => !o && onClose()}>
        <DialogContent className="max-w-lg border-border bg-surface">
          <DialogHeader>
            <DialogTitle>Start agent</DialogTitle>
            <DialogDescription>{allowed ? 'Pick a ticket first.' : 'This addon may not start agents in this workspace.'}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  const now = today.data.now
  const grant = activeGrantOf(grants.data, me.data.person, Date.parse(now))
  const canIssue = can(role, 'grant.issue')
  const until = new Date(Date.parse(now) + GRANT_HOURS * 3600_000).toISOString()
  const covers = [
    `Ticket: ${p.ticket} · ${p.title}`,
    `Mode: ${p.mode}`,
    `Harness: ${p.harness}`,
    `Where: ${p.where}`,
    ...(p.model ? [p.model] : []),
    grant ? `Runs under your grant ${grant.id} until ${hhmm(grant.until)}; revoking it stops this run` : `Issues you a grant: all tickets in this workspace, ${GRANT_HOURS} h, until ${hhmm(until)}`,
  ]
  const body = (
    <>
      {/* The exact command, wrapped so all of it is visible before it runs. */}
      <pre aria-label="Command" className="whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5 text-text">
        <code>{p.command}</code>
      </pre>
      {p.blocked && (
        <p role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-[13px] text-text">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          {p.blocked}
        </p>
      )}
    </>
  )
  const start = () => {
    onClose()
    onStart()
  }

  if (!grant && canIssue) {
    return (
      <SignPrompt
        title={`Sign a grant and start ${p.harness} on ${p.ticket}`}
        covers={covers}
        disabled={!!p.blocked}
        onClose={onClose}
        onSign={() => {
          onClose()
          void signed('Grant issued', () => api.issueGrant(ws, { hours: GRANT_HOURS, scope: 'all' })).then((ok) => ok && onStart())
        }}
      >
        {body}
      </SignPrompt>
    )
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bot className="size-4 text-brand" aria-hidden />
            {`Start ${p.harness} on ${p.ticket}`}
          </DialogTitle>
          <DialogDescription>orch starts this session under your grant. Only core shows this confirmation; an addon cannot start a session on its own.</DialogDescription>
        </DialogHeader>
        {body}
        <dl className="grid grid-cols-[88px_1fr] gap-x-3 rounded-md border border-border bg-bg p-3 text-[13px]">
          <dt className="text-text-muted">Starts</dt>
          <dd>
            <ul className="list-disc space-y-0.5 pl-4">
              {covers.slice(0, grant ? covers.length : -1).map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </dd>
        </dl>
        {!grant && <p className="text-[13px] text-text-muted">You have no active grant in this workspace. Only owners and maintainers issue grants: ask one to issue yours.</p>}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!grant || !!p.blocked} onClick={start}>
            Start agent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
