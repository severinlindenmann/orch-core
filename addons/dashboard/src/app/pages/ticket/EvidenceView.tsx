// The proving artifacts of a ticket's acceptance criteria, shown where the person decides: under each criterion on the
// Acceptance tab and inside the verdict dialog. Every preview is the one Viewer the Artifacts tab uses (same sandbox,
// same agent-HTML switch, same 64 KiB text rule): nothing here renders file content itself.
import { ChevronLeft, FileQuestion } from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { queries } from '@/api/queries'
import { workspaceOfTicket } from '@/api/workspaces'
import type { AcceptanceStatus, Artifact, TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { visible } from '@/components/sign/visible'
import { commitCover } from '@/api/gates'
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

/** Who added the file; an unknown kind reads as the stricter one (an agent). */
export const artifactKind = (a: Artifact): 'agent' | 'host' | 'person' => {
  const k = a.added_by_kind ?? (a.added_by === 'host' ? 'host' : 'agent')
  return k === 'host' || k === 'person' ? k : 'agent'
}

/**
 * Who added the file, and (for an agent) its own note, on one distinct background: an agent's evidence and its words are
 * not part of what a dialog asks the person to sign. Outside a dialog the same facts are a quiet line.
 */
function Provenance({ a, inDialog, grouped = false }: { a: Artifact; inDialog: boolean; grouped?: boolean }) {
  const kind = artifactKind(a)
  // Inside a verdict group the group's own caption says who added it: a row only quotes the agent's note.
  if (grouped) return a.label ? <p data-testid="evidence-note" className="mt-1 text-[11px] text-text-muted">{kind === 'agent' ? <>Agent&apos;s note: &ldquo;{visible(a.label)}&rdquo;</> : visible(a.label)}</p> : null
  if (kind !== 'agent')
    return (
      <p className="mt-1 text-[11px] text-text-faint">
        Added by {kind === 'host' ? 'orch' : 'a person'}.{a.label && <> {visible(a.label)}</>}
      </p>
    )
  const name = agentName(a.added_by)
  const by = name === 'An agent' ? 'an agent' : `agent ${visible(name)}`
  const note = a.label ? <> Agent&apos;s note: &ldquo;{visible(a.label)}&rdquo;</> : null
  if (!inDialog)
    return (
      <p data-testid="evidence-provenance" className="mt-1 text-[11px] text-text-faint">
        Added by {by}.{note}
      </p>
    )
  return (
    <p data-testid="evidence-provenance" className="mt-1 rounded border border-warning/30 bg-warning-soft px-2 py-1 text-[11px] text-text">
      Added by {by}.{note}
    </p>
  )
}

export function EvidenceItem({ ticket, artifact, acId, compact = false, dialog = compact, defaultOpen = openAtFirst(artifact), grouped = false, onPreview }: { ticket: TicketDocument; artifact: Artifact; acId: string; compact?: boolean; /** Inside a sign dialog (not Today's row). */ dialog?: boolean; defaultOpen?: boolean; /** In a verdict group: no per-row provenance sentence. */ grouped?: boolean; /** In the verdict dialog: the preview replaces the dialog body instead of opening here. */ onPreview?: (p: { artifact: Artifact; acId: string }) => void }) {
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
            if (onPreview) return onPreview({ artifact, acId })
            setClicked(true)
            setOpen(!open)
          }}>
          {open && !onPreview ? 'Hide preview' : 'Show preview'}
        </Button>
      </div>
      <Provenance a={artifact} inDialog={dialog} grouped={grouped} />
      {open && !onPreview && (
        <div id={body} className={cn('mt-2 overflow-auto', compact && 'max-h-64')}>
          <Viewer key={`${artifact.name}:${artifact.sha256}`} a={artifact} agentHtml={agentHtml} ticket={ticket.key} holdPictures={defaultOpen && !clicked} />
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

/** The preview of one file that replaces the verdict dialog's body ("Back to verdict" returns; the choice and note are kept by the dialog). */
export function VerdictPreview({ ticket, artifact, onBack }: { ticket: TicketDocument; artifact: Artifact; onBack: () => void }) {
  const agentHtml = useAgentHtml(ticket.key)
  return (
    <section aria-label={`Preview of ${visible(artifact.name)}`} className="min-w-0 space-y-3">
      <Button type="button" variant="ghost" size="sm" className="-ml-2 h-7 gap-1 px-2 text-[13px]" onClick={onBack}>
        <ChevronLeft aria-hidden />
        Back to verdict
      </Button>
      <p className="break-all font-mono text-[13px] font-medium">{visible(artifact.name)}</p>
      <div className="min-w-0 max-w-full overflow-x-hidden"><Viewer key={`${artifact.name}:${artifact.sha256}`} a={artifact} agentHtml={agentHtml} ticket={ticket.key} /></div>
    </section>
  )
}

/**
 * The verdict dialog's evidence, grouped by who added each file (the verdict binds every file's digest, so they are part of
 * what is signed; their CONTENT and notes are not orch's words): one labelled region per agent, a plain group per person
 * and one for orch. An unknown kind counts as an agent. Outside a dialog (Today) the rows keep a quiet provenance line.
 */
export function VerdictEvidence({ ticket, dialog = true, onPreview, name = () => 'a person' }: { ticket: TicketDocument; /** false: Today's row, not a dialog. */ dialog?: boolean; onPreview?: (p: { artifact: Artifact; acId: string }) => void; name?: (id: string) => string }) {
  const withProof = ticket.acceptance_state.map((ac) => ({ ac, items: proofArtifacts(ticket, ac) }))
  // A criterion proved by a finished check (a task receipt) has evidence too, just no file: it is not "missing".
  const byCheck = withProof.filter((x) => x.items.length === 0 && x.ac.evidence.some((ev) => ev.kind === 'task'))
  const without = withProof.filter((x) => x.items.length === 0 && x.ac.proof !== 'accepted' && !byCheck.includes(x))
  const total = withProof.reduce((n, x) => n + x.items.length, 0)
  const row = (a: Artifact, acId: string, grouped: boolean) => <EvidenceItem key={a.name + acId} ticket={ticket} artifact={a} acId={acId} compact dialog={dialog} defaultOpen={false} grouped={grouped} onPreview={onPreview} />
  // One group per identity: agents by name, people by id, orch once. Criterion headings sit above their files in the group.
  const groups = new Map<string, { id: string; kind: 'agent' | 'host' | 'person'; who: string; rows: { ac: AcceptanceStatus; items: Artifact[] }[] }>()
  if (dialog)
    for (const { ac, items } of withProof)
      for (const a of items) {
        const kind = artifactKind(a)
        const key = kind === 'host' ? 'host' : `${kind}:${a.added_by}`
        const g = groups.get(key) ?? { id: key, kind, who: kind === 'agent' ? agentName(a.added_by) : kind === 'person' ? name(a.added_by) : 'orch', rows: [] }
        const last = g.rows[g.rows.length - 1]
        if (last?.ac.id === ac.id) last.items.push(a)
        else g.rows.push({ ac, items: [a] })
        groups.set(key, g)
      }
  const ordered = [...groups.values()].sort((x, y) => Number(y.kind === 'agent') - Number(x.kind === 'agent'))
  const criterion = (ac: AcceptanceStatus) => (
    <p className="text-[12px] text-text">
      <span className="font-mono text-text-muted">{ac.id}</span> {visible(ac.text)}
    </p>
  )
  return (
    <section aria-label="Evidence" className="space-y-2 rounded-md border border-border bg-bg px-3 py-2">
      <h3 className="text-[12px] font-medium text-text-muted">
        Evidence · {total} {total === 1 ? 'file' : 'files'} for {withProof.length - without.length} of {withProof.length} {withProof.length === 1 ? 'criterion' : 'criteria'}
      </h3>
      {total === 0 && <p className="text-[12px] text-text-muted">No file is attached as proof yet.</p>}
      <div className="max-h-[40vh] space-y-3 overflow-auto">
        {dialog
          ? ordered.map((g) => {
              const body = g.rows.map(({ ac, items }) => (
                <div key={ac.id}>
                  {criterion(ac)}
                  <ul className="mt-1 space-y-1.5" aria-label={`Evidence for ${ac.id}`}>{items.map((a) => row(a, ac.id, true))}</ul>
                </div>
              ))
              return g.kind === 'agent' ? (
                <div key={g.id} role="group" aria-label={g.who === 'An agent' ? 'Written by the agent' : `Written by the agent ${visible(g.who)}`} data-testid="agent-region" className="space-y-2 rounded-md border border-info/30 bg-info-soft/40 p-2">
                  <p className="text-[12px] text-text-muted">Added by {g.who === 'An agent' ? 'an agent' : visible(g.who)}. Your verdict covers these files exactly as they are now. Their content and any quoted notes are the agent&apos;s own words.</p>
                  {body}
                </div>
              ) : (
                <div key={g.id} role="group" aria-label={g.kind === 'host' ? 'Added by orch' : `Added by ${visible(g.who)}`} data-testid="plain-group" className="space-y-2">
                  <p className="text-[12px] font-medium text-text-muted">Added by {g.kind === 'host' ? 'orch' : visible(g.who)}</p>
                  {body}
                </div>
              )
            })
          : withProof.map(({ ac, items }) =>
              items.length === 0 ? null : (
                <div key={ac.id}>
                  {criterion(ac)}
                  <ul className="mt-1 space-y-1.5" aria-label={`Evidence for ${ac.id}`}>{items.map((a) => row(a, ac.id, false))}</ul>
                </div>
              ),
            )}
      </div>
      {byCheck.length > 0 && <p className="text-[12px] text-text-muted">{byCheck.map((x) => x.ac.id).join(', ')} {byCheck.length === 1 ? 'is' : 'are'} proved by a check the agent ran (see the task receipts), not by a file.</p>}
      {without.length > 0 && <p data-testid="no-evidence" className="flex items-center gap-1.5 text-[12px] text-text-muted"><FileQuestion className="size-3.5" aria-hidden />No evidence file yet for {without.map((x) => x.ac.id).join(', ')}.</p>}
    </section>
  )
}

/** The commit a verdict covers, always visible in the verdict dialog (core shows `source_sha`; it is what the verdict binds). */
export function CommitLine({ ticket }: { ticket: TicketDocument }) {
  const b = ticket.branch
  return (
    <section aria-label="Commit" data-testid="verdict-commit" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
      <h3 className="mb-0.5 text-[12px] font-medium text-text-muted">The verdict signs this commit</h3>
      {b.head ? (
        <p className="break-words font-mono text-[12px] text-text">{visible(commitCover(b))}</p>
      ) : (
        <p className="text-text">No code is linked to this ticket: the verdict covers the criteria and the files below.</p>
      )}
      <p className="mt-1 text-[12px] text-text-muted">A new commit on the branch after the verdict voids it; the ticket goes back to testing.</p>
    </section>
  )
}
