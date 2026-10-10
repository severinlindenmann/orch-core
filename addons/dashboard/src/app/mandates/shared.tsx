// Mandates, PREVIEW ONLY (docs/concept-mandates.md, owner decision 10 Oct evening: the wide mandate). The pieces the Mandates tab, the shell banner and
// Today's digest share: the preview's query and its one request, the label, and core's dialogs for Stop and
// Revoke and void. Every request goes to the preview endpoint (api.postMandatesPreview); nothing here signs, runs
// Touch ID or posts to a signing or decision path.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Hand, ShieldCheck } from 'lucide-react'
import { useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import { queries } from '@/api/queries'
import { DECISION_KIND_LABEL, PREVIEW_LINE, revokeSplit, type MandatesPreviewRequest, type MandatesPreviewState, type PreviewMandate } from '@/api/mandatesPreview'
import { plain, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { toastApiError } from '@/app/toast'
import { useRole } from '@/app/useRole'
import { Pill } from '../pages/ticket/shared'

/** The preview's state for a workspace (the shell's loader warms it, so the banner never pops in). */
export function useMandatesPreview(ws: string | undefined, opts: { poll?: boolean } = {}) {
  return useQuery({
    ...queries.mandatesPreview(ws!),
    enabled: !!ws,
    // While a Stop waits for the host's acknowledgement, look again shortly.
    refetchInterval: (q) => (opts.poll !== false && q.state.data?.mandate?.state === 'stopping' ? 400 : false),
  })
}

/** Is the preview on with a mandate the shell and Today should show (not revoked)? */
export function shownMandate(state: MandatesPreviewState | undefined): PreviewMandate | null {
  const m = state?.on ? state.mandate : null
  return m && m.state !== 'revoked' ? m : null
}

/** Owners only (the preview stands for the owner's own mandate). */
export function useCanMandate(): boolean {
  return can(useRole(), 'settings')
}

/** The preview's one request: posts to the preview endpoint only, then puts the answer in the cache. */
export function useMandatesOp(ws: string | undefined) {
  const qc = useQueryClient()
  return async (req: MandatesPreviewRequest, done?: string): Promise<boolean> => {
    if (!ws) return false
    try {
      const state = await api.postMandatesPreview(ws, req)
      qc.setQueryData(queries.mandatesPreview(ws).queryKey, state)
      if (done) toast.success(done, { description: 'Preview — nothing was signed.' })
      return true
    } catch (e) {
      toastApiError(e, 'The preview could not do that')
      return false
    }
  }
}

/** "Preview" chip and the calm line, word for word. */
export function PreviewNote({ className, chip = true }: { className?: string; chip?: boolean }) {
  return (
    <p className={cn('flex flex-wrap items-center gap-2 text-[12px] text-text-muted', className)} data-testid="mandates-preview-note">
      {chip && <Pill>Preview</Pill>}
      <span>{PREVIEW_LINE}</span>
    </p>
  )
}

/** One mandate decision in core's words, with the checker line under it. Values go through visible.tsx (Raw / plain). */
export function DecisionLine({ m, d, className }: { m: PreviewMandate; d: PreviewMandate['decisions'][number]; className?: string }) {
  return (
    <span className={cn('min-w-0', className)}>
      {/* Core's label in full (it wraps, never truncates): it must never pass for the owner's own signature. */}
      <span className="block break-words" data-testid="decision-label">
        {DECISION_KIND_LABEL[d.kind]}: via mandate <Raw>{m.id}</Raw>, for {plain(m.issuer)} — no person reviewed this
        {d.commit && (
          <>
            {' '}
            (commit <Raw>{d.commit}</Raw>)
          </>
        )}
      </span>
      {d.target && <span className="block break-words text-text">{plain(d.target)}</span>}
      <span className="block truncate text-text-muted">
        checked by checker <Raw>{d.checker.identity}</Raw> ({d.checker.result})
      </span>
    </span>
  )
}

/** What a decision is on: the ticket key (exact), or "workspace" for a workspace-level decision. */
export function DecisionSubject({ d, className }: { d: { ticket?: string }; className?: string }) {
  return <span className={cn('shrink-0', className)}>{d.ticket ? <Raw>{d.ticket}</Raw> : <span className="text-text-muted">workspace</span>}</span>
}

/** Core's preview dialog: title, the preview line, Covers, then a confirm that says nothing is signed. */
export function PreviewPrompt({
  title,
  covers,
  children,
  destructive,
  confirmLabel,
  disabled,
  onConfirm,
  onClose,
  icon = 'shield',
}: {
  title: string
  covers: ReactNode[]
  children?: ReactNode
  destructive?: boolean
  confirmLabel: string
  disabled?: boolean
  onConfirm: () => void
  onClose: () => void
  icon?: 'shield' | 'stop' | 'void'
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  const Icon = icon === 'stop' ? Hand : icon === 'void' ? Ban : ShieldCheck
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        role={destructive ? 'alertdialog' : 'dialog'}
        className="max-h-[90vh] max-w-lg grid-cols-[minmax(0,1fr)] gap-4 overflow-y-auto border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          cancel.current?.focus({ preventScroll: true })
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Icon className={cn('size-4', destructive ? 'text-danger' : 'text-brand')} />
            {title}
          </DialogTitle>
          <DialogDescription className="flex flex-wrap items-center gap-2">
            <Pill>Preview</Pill>
            <span>{PREVIEW_LINE}</span>
          </DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 rounded-md border border-border bg-bg p-3 text-[13px]">
          <dt className="text-text-muted">Covers</dt>
          <dd>
            <ul className="list-disc space-y-0.5 pl-4" data-testid="preview-covers">
              {covers.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </dd>
        </dl>
        {children}
        <DialogFooter className="flex-wrap gap-2">
          <Button ref={cancel} variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={destructive ? 'destructive' : 'default'} disabled={disabled} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Stop (concept §2.13): the host acknowledges with a boundary; nothing after it is signed. */
export function StopDialog({ ws, m, onClose }: { ws: string; m: PreviewMandate; onClose: () => void }) {
  const [agents, setAgents] = useState(false)
  const op = useMandatesOp(ws)
  return (
    <PreviewPrompt
      title={`Stop mandate ${plain(m.id)}?`}
      icon="stop"
      covers={[
        <>Mandate <Raw>{m.id}</Raw>, for {plain(m.issuer)}, whole workspace</>,
        'Nothing after the boundary the host acknowledges is signed',
        'Queued effects end: landing entries are dequeued, pending checker runs end',
        agents ? <>Also stops the agents: <Raw>{m.orchestrator.name}</Raw> (<Raw>{m.orchestrator.identity}</Raw>) and its subagents; its grant is revoked</> : 'The agents keep running (without the mandate)',
      ]}
      confirmLabel="Stop mandate (preview — nothing is signed)"
      onConfirm={() => {
        onClose()
        void op({ op: 'stop', stop_agents: agents }, `Stop sent for ${plain(m.id)}`)
      }}
      onClose={onClose}
    >
      <label className="flex items-center gap-2 text-[13px]">
        <Checkbox checked={agents} onCheckedChange={(v) => setAgents(v === true)} aria-label="Also stop agents" />
        Also stop agents
      </label>
      {agents && <p className="text-[12px] text-text-muted">Preview: no agent session is stopped.</p>}
    </PreviewPrompt>
  )
}

/** Revoke and void (concept §2.13): every mandate decision on work not landed is voided; landed work is listed. */
export function RevokeDialog({ ws, m, onClose }: { ws: string; m: PreviewMandate; onClose: () => void }) {
  const op = useMandatesOp(ws)
  const { voids, landed } = revokeSplit(m)
  return (
    <PreviewPrompt
      title={`Revoke mandate ${plain(m.id)} and void its decisions?`}
      icon="void"
      destructive
      covers={[
        <>Revokes mandate <Raw>{m.id}</Raw>, for {plain(m.issuer)}, whole workspace</>,
        `Voids ${voids.length} decision${voids.length === 1 ? '' : 's'} on work that has not landed (gate.invalidated, cause: mandate revoked)`,
        landed.length ? `Lists ${landed.length} decision${landed.length === 1 ? '' : 's'} on landed work for your review (not voided)` : 'No decision covers landed work',
      ]}
      confirmLabel="Revoke and void (preview — nothing is signed)"
      onConfirm={() => {
        onClose()
        void op({ op: 'revoke' }, `Mandate ${plain(m.id)} revoked, ${voids.length} decisions voided`)
      }}
      onClose={onClose}
    >
      <section aria-label="Decisions that would be voided" className="space-y-1">
        <h3 className="text-[12px] font-semibold text-text">Would be voided ({voids.length})</h3>
        <ul className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-border p-2 text-[12px]" data-testid="revoke-voids">
          {voids.map((d) => (
            <li key={d.id} className="flex min-w-0 gap-2">
              <span className="shrink-0 font-mono text-text-muted">#{d.seq}</span>
              <DecisionSubject d={d} />
              <DecisionLine m={m} d={d} />
            </li>
          ))}
        </ul>
      </section>
      {landed.length > 0 && (
        <section aria-label="Landed work for your review" className="space-y-1">
          <h3 className="text-[12px] font-semibold text-text">Landed, listed for your review ({landed.length})</h3>
          <ul className="space-y-1 rounded-md border border-border p-2 text-[12px]" data-testid="revoke-landed">
            {landed.map((d) => (
              <li key={d.id} className="flex min-w-0 gap-2">
                <span className="shrink-0 font-mono text-text-muted">#{d.seq}</span>
                <DecisionSubject d={d} />
                <DecisionLine m={m} d={d} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </PreviewPrompt>
  )
}

/** "until Tue" for the banner; the exact instant stays in the Mandates tab. */
export function weekday(iso: string): string {
  return new Date(iso).toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'UTC' })
}
