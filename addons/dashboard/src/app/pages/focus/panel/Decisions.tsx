import { useState } from 'react'
import { canAct } from '@/api/features'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { SignDialog } from '../../ticket/SignDialog'
import { availableActions, canAnswer, GATE_LABEL } from '../../ticket/actions'
import { fmtTime, useViewer, type HumanAction } from '../../ticket/shared'
import { decisionGates, Empty, usePanelTicket } from './data'

export function Decisions({ ticket }: { ticket?: string }) {
  const { data, workspace, isError } = usePanelTicket(ticket)
  const viewer = useViewer(ticket ?? '')
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [action, setAction] = useState<HumanAction | null>(null)
  if (!ticket) return <Empty>No ticket yet. Decisions will appear here.</Empty>
  if (isError) return <Empty>Could not load decisions.</Empty>
  if (!data) return <Empty>Loading decisions…</Empty>
  const available = availableActions(data, viewer)
  const active = viewer.ready && canAct(workspace?.id)
  return <div data-testid="focus-panel-decisions" className="space-y-4">
    {!data.questions_state.length && <p className="text-sm text-text-muted">No questions yet.</p>}
    {[...data.questions_state].sort((a, b) => Number(b.state === 'open') - Number(a.state === 'open')).map(q => <section key={q.id} className="space-y-2 rounded-lg border border-border bg-surface p-3">
      <p className="text-xs text-text-muted">{q.state === 'open' ? 'Open question' : q.state === 'stale' ? 'Question changed since the answer' : 'Answered'}</p>
      <h3 className="break-words text-sm font-medium">{q.text}</h3>
      {q.why && <p className="text-xs text-text-muted">{q.why}</p>}
      {q.answer && q.state !== 'stale' && <><p className="text-sm text-success">{q.options?.find(o => o.key === q.answer?.option)?.label ?? q.answer.text ?? 'Answer recorded'}</p><p className="text-xs text-text-muted">{viewer.name(q.answer.by)}{q.answer.at && ` · ${fmtTime(q.answer.at)}`}</p></>}
      {active && canAnswer(q, viewer) && <><Textarea aria-label={`Your answer to ${q.text}`} placeholder={q.options?.length ? 'Add a note or write an answer' : 'Your answer'} rows={2} value={answers[q.id] ?? ''} onChange={e => setAnswers({ ...answers, [q.id]: e.target.value })} /><div className="flex flex-wrap gap-2">{q.options?.map(o => <Button key={o.key} size="sm" variant="outline" onClick={() => setAction({ kind: 'answer', question: q.id, option: o.key, text: answers[q.id]?.trim() || undefined })}>{o.label}</Button>)}<Button size="sm" variant="outline" disabled={!answers[q.id]?.trim()} onClick={() => setAction({ kind: 'answer', question: q.id, text: answers[q.id].trim() })}>Send written answer</Button></div></>}
      {q.state === 'open' && !canAnswer(q, viewer) && <p className="text-xs text-text-muted">Waiting for {viewer.name(q.to)}.</p>}
    </section>)}
    {decisionGates(data).map(gate => {
      const g = data.gates[gate]
      return <section key={gate} className="space-y-2 border-t border-border pt-3">
        <h3 className="text-sm font-medium">{GATE_LABEL[gate]}</h3><p className="text-xs text-text-muted">{g.state.replaceAll('_', ' ')}</p>
        {g.approvals.map((a, i) => <p key={i} className="text-xs text-text-muted">Approved by {viewer.name(a.by)} · {fmtTime(a.at)}</p>)}
        {g.note && <p className="text-sm">{g.note}</p>}
        {gate === 'verify' && data.verdict && <p className="text-sm">{data.verdict.result === 'pass' ? 'Passed' : 'Changes requested'} by {viewer.name(data.verdict.by)} · {fmtTime(data.verdict.at)}</p>}
        {active && <div className="flex flex-wrap gap-2">{gate !== 'verify' && available.approve.includes(gate) && <Button size="sm" variant="outline" onClick={() => setAction({ kind: 'approve', gate })}>Approve {GATE_LABEL[gate].toLowerCase()}</Button>}{gate === 'verify' && available.verdict && <Button size="sm" variant="outline" onClick={() => setAction({ kind: 'verdict' })}>Give verdict</Button>}{available.requestChanges.includes(gate) && <Button size="sm" variant="outline" onClick={() => setAction({ kind: 'request_changes', gate })}>Request changes</Button>}</div>}
      </section>
    })}
    {action && <SignDialog ticket={data} action={action} onClose={() => setAction(null)} />}
  </div>
}
