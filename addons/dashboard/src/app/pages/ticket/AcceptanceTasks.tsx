import { BadgeCheck, CircleDashed, CircleDot, FileText, ListChecks, Terminal } from 'lucide-react'
import type { AcceptanceStatus, TaskStatus, TicketDocument } from '@/api/types'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { agentName, fmtDuration, Mono, Pill, Section, type Jump, type TabProps } from './shared'

type AcState = 'open' | 'evidenced' | 'done'

export function acState(ticket: TicketDocument, ac: AcceptanceStatus): AcState {
  if (ac.state === 'unproven') return 'open'
  return ticket.status === 'done' || ticket.gates.verify.state === 'approved' ? 'done' : 'evidenced'
}

const AC_UI: Record<AcState, { label: string; tone: 'neutral' | 'info' | 'success'; Icon: typeof CircleDot }> = {
  open: { label: 'Open', tone: 'neutral', Icon: CircleDashed },
  evidenced: { label: 'Evidenced', tone: 'info', Icon: CircleDot },
  done: { label: 'Done', tone: 'success', Icon: BadgeCheck },
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

function Evidence({ ac, ticket, jump }: { ac: AcceptanceStatus; ticket: TicketDocument; jump: (j: Jump) => void }) {
  if (ac.evidence.length === 0) return <p className="mt-2 text-[12px] text-text-faint">No evidence yet.</p>
  return (
    <ul className="mt-2 flex flex-wrap gap-1.5" aria-label={`Proven by, ${ac.id}`}>
      {ac.evidence.map((ev) => {
        if (ev.kind === 'task') {
          const t = ticket.tasks_state.find((x) => x.id === ev.ref)
          return (
            <li key={'t' + ev.ref}>
              <button
                type="button"
                onClick={() => jump({ tab: 'acceptance', id: `task-${ev.ref}` })}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[12px] hover:border-border-strong hover:bg-surface-2"
              >
                <ListChecks className="size-3.5 text-text-muted" />
                <Mono>{ev.ref}</Mono>
                <span className="text-success">verified by receipt</span>
                {t?.receipt && <span className="text-text-faint">exit {t.receipt.exit}</span>}
              </button>
            </li>
          )
        }
        const art = ticket.artifacts.find((a) => a.name === ev.ref)
        const verified = art?.kind === 'receipt'
        return (
          <li key={'a' + ev.ref}>
            <button
              type="button"
              onClick={() => jump({ tab: 'artifacts', id: `artifact-${ev.ref}` })}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[12px] hover:border-border-strong hover:bg-surface-2"
            >
              <FileText className="size-3.5 text-text-muted" />
              <span className="max-w-48 truncate">{ev.ref}</span>
              <span className={verified ? 'text-success' : 'text-warning'}>{verified ? 'verified by receipt' : 'agent-asserted'}</span>
            </button>
          </li>
        )
      })}
    </ul>
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
            {ticket.acceptance_state.map((ac) => {
              const st = acState(ticket, ac)
              const ui = AC_UI[st]
              return (
                <li key={ac.id} id={`ac-${ac.id}`} className="py-3 first:pt-0 last:pb-0" data-state={st}>
                  <div className="flex items-start gap-3">
                    <Mono className="mt-0.5 w-8 shrink-0 text-text-muted">{ac.id}</Mono>
                    <p className="min-w-0 flex-1 text-[13px] leading-relaxed">
                      <Inline text={ac.text} />
                    </p>
                    <Pill tone={ui.tone}>
                      <ui.Icon />
                      {ui.label}
                    </Pill>
                  </div>
                  <div className="pl-11">
                    <Evidence ac={ac} ticket={ticket} jump={jump} />
                  </div>
                </li>
              )
            })}
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
                <TableHead>Verify</TableHead>
                <TableHead>Receipt</TableHead>
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
                  </TableCell>
                  <TableCell>
                    <Pill tone={TASK_TONE[t.state]}>{TASK_LABEL[t.state]}</Pill>
                  </TableCell>
                  <TableCell className="whitespace-normal text-[12px] text-text-muted">
                    {t.assignee && <div className="text-text">{viewer.name(t.assignee)}</div>}
                    {t.lease ? <div>leased to {leaseText(t)}</div> : !t.assignee && <span className="text-text-faint">unassigned</span>}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    {t.verify ? (
                      <span className="inline-flex items-start gap-1 break-all font-mono text-[12px] text-text-muted">
                        <Terminal className="mt-0.5 size-3 shrink-0" />
                        {t.verify.cmd}
                      </span>
                    ) : (
                      <span className="text-[12px] text-text-faint">manual</span>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-normal text-[12px]">
                    {t.receipt ? (
                      <span className="font-mono text-text-muted">
                        exit <span className={t.receipt.exit === 0 ? 'text-success' : 'text-danger'}>{t.receipt.exit}</span> · {fmtDuration(t.receipt.ms)}
                        {t.receipt.commit && (
                          <>
                            {' · '}
                            <span className="text-text">{t.receipt.commit}</span>
                          </>
                        )}
                      </span>
                    ) : (
                      <span className="text-text-faint">none</span>
                    )}
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
