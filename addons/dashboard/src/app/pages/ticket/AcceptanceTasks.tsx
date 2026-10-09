import { ChevronDown, ChevronRight, FileText, ListChecks } from 'lucide-react'
import { useState } from 'react'
import type { AcceptanceStatus, TaskStatus, TicketDocument } from '@/api/types'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { agentName, fmtDuration, Mono, Pill, Section, type Jump, type TabProps } from './shared'

type AcState = 'open' | 'evidenced' | 'done'

export function acState(ticket: TicketDocument, ac: AcceptanceStatus): AcState {
  if (ac.state === 'unproven') return 'open'
  return ticket.status === 'done' || ticket.gates.verify.state === 'approved' ? 'done' : 'evidenced'
}

const TASK_TONE: Record<TaskStatus['state'], 'neutral' | 'info' | 'success' | 'warning'> = {
  todo: 'neutral',
  doing: 'info',
  done: 'success',
  blocked: 'warning',
  skipped: 'neutral',
}
const TASK_LABEL: Record<TaskStatus['state'], string> = { todo: 'To do', doing: 'Doing', done: 'Done', blocked: 'Blocked', skipped: 'Skipped' }

function Code({ text }: { text: string }) {
  return <code className="rounded bg-surface-3 px-1 py-0.5 text-[12px]">{text}</code>
}
/** Inline code in a one-line string (`like this`). */
function Inline({ text }: { text: string }) {
  return (
    <>
      {text.split(/(`[^`]+`)/g).map((p, i) => (p.startsWith('`') ? <Code key={i} text={p.slice(1, -1)} /> : <span key={i}>{p}</span>))}
    </>
  )
}

type Proof = 'proven' | 'asserted' | 'pending'

const PROOF_UI: Record<Proof, { glyph: string; label: string; cls: string }> = {
  proven: { glyph: '✓', label: 'Proven by a receipt', cls: 'text-success' },
  asserted: { glyph: '◐', label: 'Agent-asserted', cls: 'text-warning' },
  pending: { glyph: '○', label: 'Pending', cls: 'text-text-muted' },
}

/** Task evidence is a receipt the host recorded; an artifact counts only when it is a receipt. Anything else is the agent's claim. */
function evidenceVerified(ticket: TicketDocument, ev: AcceptanceStatus['evidence'][number]): boolean {
  if (ev.kind === 'task') return true
  return ticket.artifacts.find((a) => a.name === ev.ref)?.kind === 'receipt'
}

function proofOf(ticket: TicketDocument, ac: AcceptanceStatus): Proof {
  if (ac.evidence.length === 0) return 'pending'
  return ac.evidence.some((ev) => evidenceVerified(ticket, ev)) ? 'proven' : 'asserted'
}

function ReceiptLine({ t }: { t: TaskStatus }) {
  if (!t.receipt) return <span className="text-text-faint">No receipt yet.</span>
  return (
    <span className="font-mono text-text-muted">
      exit <span className={t.receipt.exit === 0 ? 'text-success' : 'text-danger'}>{t.receipt.exit}</span> · {fmtDuration(t.receipt.ms)}
      {t.receipt.commit && (
        <>
          {' · '}
          <span className="text-text">{t.receipt.commit}</span>
        </>
      )}
    </span>
  )
}

/** "How it's verified": the command in a code block and its receipt. Closed until asked for. */
function HowVerified({ t }: { t: TaskStatus }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-1">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 text-[12px] text-text-muted hover:text-text">
        {open ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
        How it&apos;s verified
      </button>
      {open && (
        <div className="mt-1 space-y-1 text-[12px]">
          {t.verify ? <CodeBlock language="shellscript" text={t.verify.cmd} /> : <p className="text-text-faint">Checked by a person, no command.</p>}
          <ReceiptLine t={t} />
        </div>
      )}
    </div>
  )
}

/** One line per criterion: glyph, text, an Evidence link. The detail (what proves it) opens on demand. */
function Criterion({ ac, ticket, jump }: { ac: AcceptanceStatus; ticket: TicketDocument; jump: (j: Jump) => void }) {
  const [open, setOpen] = useState(false)
  const proof = proofOf(ticket, ac)
  const ui = PROOF_UI[proof]
  const first = ac.evidence[0]
  const goto = (ev: typeof first) => jump(ev.kind === 'task' ? { tab: 'acceptance', id: `task-${ev.ref}` } : { tab: 'artifacts', id: `artifact-${ev.ref}` })
  return (
    <>
      <div className="flex items-start gap-3">
        <span role="img" aria-label={ui.label} title={ui.label} className={`mt-px w-4 shrink-0 text-center text-[14px] leading-5 ${ui.cls}`}>
          {ui.glyph}
        </span>
        <Mono className="mt-0.5 w-8 shrink-0 text-text-muted">{ac.id}</Mono>
        <p className="min-w-0 flex-1 text-[13px] leading-relaxed">
          <Inline text={ac.text} />
        </p>
        {first ? (
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="shrink-0 text-[12px] text-brand underline-offset-2 hover:underline">
            Evidence
          </button>
        ) : (
          <span className="shrink-0 text-[12px] text-text-faint">No evidence yet</span>
        )}
      </div>
      {open && first && (
        <ul className="mt-2 space-y-1.5 pl-[3.75rem]" aria-label={`Proven by, ${ac.id}`}>
          {ac.evidence.map((ev) => {
            const verified = evidenceVerified(ticket, ev)
            const t = ev.kind === 'task' ? ticket.tasks_state.find((x) => x.id === ev.ref) : undefined
            return (
              <li key={ev.kind + ev.ref} className="text-[12px]">
                <button type="button" onClick={() => goto(ev)} className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 hover:border-border-strong hover:bg-surface-2">
                  {ev.kind === 'task' ? <ListChecks className="size-3.5 text-text-muted" /> : <FileText className="size-3.5 text-text-muted" />}
                  <span className="max-w-64 truncate">{ev.ref}</span>
                  <span className={verified ? 'text-success' : 'text-warning'}>{verified ? 'verified by receipt' : 'agent-asserted'}</span>
                </button>
                {t && <HowVerified t={t} />}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

function leaseText(t: TaskStatus): string {
  if (!t.lease) return ''
  const sub = t.lease.session.split('.').slice(1).map((n) => `sub${n}`).join('')
  return `${agentName(t.lease.agent)}${sub ? ' ' + sub : ''}`
}

export function AcceptanceTasks({ ticket, viewer, jump }: TabProps) {
  return (
    <div className="space-y-5">
      <Section title={`Acceptance criteria (${ticket.acceptance_state.filter((a) => a.state === 'proven').length}/${ticket.acceptance_state.length} evidenced)`}>
        {ticket.acceptance_state.length === 0 ? (
          <p className="text-[13px] text-text-faint">No acceptance criteria yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {ticket.acceptance_state.map((ac) => (
              <li key={ac.id} id={`ac-${ac.id}`} className="py-2.5 first:pt-0 last:pb-0" data-state={acState(ticket, ac)} data-proof={proofOf(ticket, ac)}>
                <Criterion ac={ac} ticket={ticket} jump={jump} />
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={`Tasks (${ticket.tasks_state.filter((t) => t.state === 'done').length}/${ticket.tasks_state.length} done)`}>
        {ticket.tasks_state.length === 0 ? (
          <p className="text-[13px] text-text-faint">{ticket.type === 'epic' ? 'Epics have no tasks; work happens in the children.' : 'No tasks yet. The plan is not broken down.'}</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-10">Id</TableHead>
                <TableHead>Task</TableHead>
                <TableHead className="w-24">State</TableHead>
                <TableHead>Assignee and lease</TableHead>
                <TableHead>Proves</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ticket.tasks_state.map((t) => (
                <TableRow key={t.id} id={`task-${t.id}`} data-state={t.state} className="align-top">
                  <TableCell>
                    <Mono className="text-text-muted">{t.id}</Mono>
                  </TableCell>
                  <TableCell className="whitespace-normal text-[13px]">
                    <Inline text={t.text} />
                    <HowVerified t={t} />
                  </TableCell>
                  <TableCell>
                    <Pill tone={TASK_TONE[t.state]}>{TASK_LABEL[t.state]}</Pill>
                  </TableCell>
                  <TableCell className="whitespace-normal text-[12px] text-text-muted">
                    {t.assignee && <div className="text-text">{viewer.name(t.assignee)}</div>}
                    {t.lease ? <div>leased to {leaseText(t)}</div> : !t.assignee && <span className="text-text-faint">unassigned</span>}
                  </TableCell>
                  <TableCell>
                    {t.proves.length ? (
                      <span className="flex flex-wrap gap-1">
                        {t.proves.map((id) => (
                          <button
                            key={id}
                            type="button"
                            onClick={() => jump({ tab: 'acceptance', id: `ac-${id}` })}
                            className="rounded border border-border px-1.5 font-mono text-[11px] text-text-muted hover:bg-surface-2"
                          >
                            {id}
                          </button>
                        ))}
                      </span>
                    ) : (
                      <span className="text-[12px] text-text-faint">none</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Section>
    </div>
  )
}
