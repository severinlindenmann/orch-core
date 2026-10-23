// The proving artifacts of a ticket's acceptance criteria, shown where the person decides: under each criterion on the
// Acceptance tab and inside the verdict dialog. Every preview is the one Viewer the Artifacts tab uses (same sandbox,
// same agent-HTML switch, same 64 KiB text rule): nothing here renders file content itself.
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { queries } from '@/api/queries'
import { workspaceOfTicket } from '@/api/workspaces'
import type { AcceptanceStatus, Artifact, TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { visible } from '@/components/sign/visible'
import { agentName, fmtBytes, Pill } from './shared'
import { Viewer } from './Artifacts'

/** The artifacts a criterion lists as its proof (tasks are receipts, shown by the task rows). */
export function proofArtifacts(ticket: TicketDocument, ac: AcceptanceStatus): Artifact[] {
  return ac.evidence.flatMap((ev) => (ev.kind === 'artifact' ? ticket.artifacts.filter((a) => a.name === ev.ref) : []))
}

/** Whether agent HTML may be drawn in this ticket's workspace (the same switch as the Artifacts tab). */
function useAgentHtml(ticketKey: string): boolean {
  const workspaces = useQuery(queries.workspaces())
  return addonActive(workspaceOfTicket(ticketKey, workspaces.data ?? []), 'widgets')
}

/** One proving artifact: its name, what it proves, and its preview (open at first, one click to fold). */
/** Files up to this size open on their own; a bigger one waits for a click (nothing big is fetched just by opening a tab). */
export const AUTO_OPEN_BYTES = 256 * 1024
/** PNG, WebP and JPEG (and text, pages) open on their own; an SVG or GIF waits for a click, whatever size it has. */
const openAtFirst = (a: Artifact) => a.bytes != null && a.bytes <= AUTO_OPEN_BYTES && !/\.(svg|gif)$/i.test(a.name)

/**
 * Who added the file, and (for an agent) its own note, on one distinct background: an agent's evidence and its words are
 * not part of what a dialog asks the person to sign. Outside a dialog the same facts are a quiet line.
 */
function Provenance({ a, inDialog }: { a: Artifact; inDialog: boolean }) {
  const kind = a.added_by_kind ?? (a.added_by === 'host' ? 'host' : 'agent') // unknown reads as the stricter one
  if (kind !== 'agent')
    return (
      <p className="mt-1 text-[11px] text-text-faint">
        Added by {kind === 'host' ? 'orch' : 'a person'}.{a.label && <> {visible(a.label)}</>}
      </p>
    )
  const who = visible(agentName(a.added_by))
  const note = a.label ? <> Agent&apos;s note: &ldquo;{visible(a.label)}&rdquo;</> : null
  if (!inDialog)
    return (
      <p data-testid="evidence-provenance" className="mt-1 text-[11px] text-text-faint">
        Added by agent {who}.{note}
      </p>
    )
  return (
    <p data-testid="evidence-provenance" className="mt-1 rounded border border-warning/30 bg-warning-soft px-2 py-1 text-[11px] text-text">
      Added by agent {who} · not part of this dialog, not what you sign.{note}
    </p>
  )
}

export function EvidenceItem({ ticket, artifact, acId, compact = false, dialog = compact, defaultOpen = openAtFirst(artifact) }: { ticket: TicketDocument; artifact: Artifact; acId: string; compact?: boolean; /** Inside a sign dialog (not Today's row): the "not part of this dialog" wording. */ dialog?: boolean; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  // Opened by itself and never clicked: an SVG or GIF (by the server's typed kind) still waits for a click.
  const [clicked, setClicked] = useState(false)
  const agentHtml = useAgentHtml(ticket.key)
  const body = `evidence-${acId}-${artifact.name}`
  return (
    <li data-testid="evidence-item" data-artifact={artifact.name} className="rounded-md border border-border bg-surface p-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
        <span className="min-w-0 truncate font-mono text-[12px] font-medium" title={visible(artifact.name)}>
          {visible(artifact.name)}
        </span>
        <Pill tone="neutral">proves {acId}</Pill>
        {!!artifact.bytes && <span className="text-text-faint">{fmtBytes(artifact.bytes)}</span>}
        <Button type="button" variant="ghost" size="sm" className="ml-auto h-6 px-2 text-[12px]" aria-expanded={open} aria-controls={body} aria-label={`${open ? 'Hide' : 'Show'} preview of ${visible(artifact.name)}`} onClick={() => {
            setClicked(true)
            setOpen(!open)
          }}>
          {open ? 'Hide preview' : 'Show preview'}
        </Button>
      </div>
      <Provenance a={artifact} inDialog={dialog} />
      {open && (
        <div id={body} className={cn('mt-2 overflow-auto', compact && 'max-h-64')}>
          <Viewer a={artifact} agentHtml={agentHtml} ticket={ticket.key} holdPictures={defaultOpen && !clicked} />
        </div>
      )}
    </li>
  )
}

/** Under one criterion: its proving artifacts with previews. Renders nothing when it lists none. */
export function CriterionEvidence({ ticket, ac }: { ticket: TicketDocument; ac: AcceptanceStatus }) {
  const items = proofArtifacts(ticket, ac)
  if (items.length === 0) return null
  return (
    <ul className="mt-2 space-y-2 pl-7 sm:pl-[3.75rem]" aria-label={`Evidence for ${ac.id}`}>
      {items.map((a) => (
        <EvidenceItem key={a.name} ticket={ticket} artifact={a} acId={ac.id} />
      ))}
    </ul>
  )
}

/**
 * The verdict dialog's evidence: a one-line summary, then each criterion with what proves it. Previews are folded at
 * first (a dialog is short) and open on a click, so Pass or Send back is decided on the evidence itself.
 */
export function VerdictEvidence({ ticket, dialog = true }: { ticket: TicketDocument; /** false: Today's row, not a dialog. */ dialog?: boolean }) {
  const withProof = ticket.acceptance_state.map((ac) => ({ ac, items: proofArtifacts(ticket, ac) }))
  // A criterion proved by a finished check (a task receipt) has evidence too, just no file: it is not "missing".
  const byCheck = withProof.filter((x) => x.items.length === 0 && x.ac.evidence.some((ev) => ev.kind === 'task'))
  const without = withProof.filter((x) => x.items.length === 0 && x.ac.proof !== 'accepted' && !byCheck.includes(x))
  const total = withProof.reduce((n, x) => n + x.items.length, 0)
  return (
    <section aria-label="Evidence" className="space-y-2 rounded-md border border-border bg-bg px-3 py-2">
      <h3 className="text-[12px] font-medium text-text-muted">
        Evidence · {total} {total === 1 ? 'file' : 'files'} for {withProof.length - without.length} of {withProof.length} {withProof.length === 1 ? 'criterion' : 'criteria'}
      </h3>
      {total === 0 && <p className="text-[12px] text-text-muted">No file is attached as proof yet.</p>}
      <div className="max-h-[40vh] space-y-2 overflow-auto">
      {withProof.map(({ ac, items }) =>
        items.length === 0 ? null : (
          <div key={ac.id}>
            <p className="text-[12px] text-text">
              <span className="font-mono text-text-muted">{ac.id}</span> {visible(ac.text)}
            </p>
            <ul className="mt-1 space-y-1.5" aria-label={`Evidence for ${ac.id}`}>
              {items.map((a) => (
                <EvidenceItem key={a.name} ticket={ticket} artifact={a} acId={ac.id} compact dialog={dialog} defaultOpen={false} />
              ))}
            </ul>
          </div>
        ),
      )}
      </div>
      {byCheck.length > 0 && <p className="text-[12px] text-text-muted">{byCheck.map((x) => x.ac.id).join(', ')} {byCheck.length === 1 ? 'is' : 'are'} proved by a check the agent ran (see the task receipts), not by a file.</p>}
      {without.length > 0 && <p className="text-[12px] text-warning">No evidence file yet for {without.map((x) => x.ac.id).join(', ')}.</p>}
    </section>
  )
}
