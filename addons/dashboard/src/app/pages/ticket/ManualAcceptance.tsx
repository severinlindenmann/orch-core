import { createContext, useContext, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { workspaceOfTicket } from '@/api/workspaces'
import { handoffMode } from '@/api/features'
import type { HandoffAction } from '@/api/handoff'
import type { AcceptanceStatus, TaskStatus, TicketDocument } from '@/api/types'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { canEditTicket } from './EditTicket'
import type { Viewer } from './shared'

type Choice = { kind: 'accept' | 'withdraw'; ac: AcceptanceStatus } | { kind: 'done'; task: TaskStatus }
const Context = createContext<((choice: Choice) => void) | null>(null)
export function mayAccept(ticket: TicketDocument, viewer: Viewer, handoff: boolean) {
  return canEditTicket(ticket, viewer.role, handoff) && !!viewer.person &&
    (viewer.person === ticket.people.owner || ticket.people.reviewers.includes(viewer.person) || viewer.role === 'owner' || viewer.role === 'maintainer')
}
export function ManualAcceptanceProvider({ ticket, viewer, children }: { ticket: TicketDocument; viewer: Viewer; children: ReactNode }) {
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  const [choice, setChoice] = useState<Choice | null>(null)
  return <Context.Provider value={mayAccept(ticket, viewer, !!ws && handoffMode(ws.id)) ? setChoice : null}>
    {children}
    {choice && <ManualDialog ticket={ticket} choice={choice} ws={ws?.id} onClose={() => setChoice(null)} />}
  </Context.Provider>
}
export function ManualButtons({ ac, task }: { ac?: AcceptanceStatus; task?: TaskStatus }) {
  const choose = useContext(Context)
  if (!choose) return null
  if (task) return task.state !== 'done' && task.state !== 'skipped' ? <Button variant="ghost" size="sm" onClick={() => choose({ kind: 'done', task })}>Mark done</Button> : null
  if (!ac) return null
  return <>
    {(!ac.accepted || ac.accepted_stale) && (ac.accepted_stale || !ac.evidence?.length) && <Button variant="ghost" size="sm" onClick={() => choose({ kind: 'accept', ac })}>{ac.accepted_stale ? 'Accept again' : 'Accept manually'}</Button>}
    {ac.accepted && <Button variant="ghost" size="sm" onClick={() => choose({ kind: 'withdraw', ac })}>Withdraw</Button>}
  </>
}
export function ManualDialog({ ticket, choice, ws, onClose }: { ticket: TicketDocument; choice: Choice; ws?: string; onClose: () => void }) {
  const [text, setText] = useState('')
  const note = text.trim()
  const title = choice.kind === 'accept' ? 'Accept manually' : choice.kind === 'withdraw' ? 'Withdraw acceptance' : 'Mark done'
  const action: HandoffAction = choice.kind === 'done' ? { action: 'mark_task_done', key: ticket.key, task: choice.task.id }
    : choice.kind === 'accept' ? { action: 'accept_criterion', key: ticket.key, ac: choice.ac.id, ...(note ? { note } : {}) }
    : { action: 'withdraw_acceptance', key: ticket.key, ac: choice.ac.id, reason: note }
  return <HandoffDialog title={title} ws={ws} action={action} confirmLabel={title} onClose={onClose}
    hint={choice.kind === 'withdraw' && !note ? 'Give a reason to continue' : note.length > 1000 ? 'Use at most 1000 characters' : null}>
    <Prose>{choice.kind === 'done' ? choice.task.text : choice.ac.text}</Prose>
    {choice.kind === 'done' ? <>
      <p>No receipt is recorded, so this proves no criterion. Accepting a criterion is a separate step.</p>
      {choice.task.lease && <p>An agent is working on it; marking it done ends its lease.</p>}
    </> : <>
      {choice.kind === 'accept' && <p>No automated check backs this: you vouch for it yourself.</p>}
      <Label htmlFor="manual-note">{choice.kind === 'accept' ? 'Note (optional)' : 'Reason (required)'}</Label>
      <Textarea id="manual-note" maxLength={1000} value={text} onChange={e => setText(e.target.value)} />
    </>}
  </HandoffDialog>
}

/** Ticket context before choosing a verdict; host material remains the signing authority. */
export function ManualAcceptanceReview({ ticket, name }: { ticket: TicketDocument; name: (id: string) => string }) {
  return <ul className="space-y-1 text-[13px] text-text-muted" aria-label="Manual acceptance">
    {ticket.acceptance_state.filter(a => a.accepted_stale || a.proof === 'accepted').map(a => <li key={a.id}>
      <Raw>{a.id}</Raw>: {a.accepted_stale ? 'accepted for an earlier wording, no longer counts' : <>accepted manually by <Raw>{a.accepted ? name(a.accepted.by) : 'someone'}</Raw> (no automated check)</>}
    </li>)}
  </ul>
}
