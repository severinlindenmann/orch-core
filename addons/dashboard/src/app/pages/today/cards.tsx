import { useState, type ReactNode } from 'react'
import { Link } from '@tanstack/react-router'
import { Check, Copy, FileSearch, Fingerprint, HelpCircle, Loader2, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { AddonDecision, GateName, NeedsYouItem, TicketDocument } from '@/api/types'
import { AddonFrame } from '@/addon-ui'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { ago, displayName, shortHash, useAct, type Directory } from './shared'

interface CommonProps {
  dir: Directory
  now: string
  readOnly: boolean
}

const PRIMARY = 'bg-brand text-on-brand hover:bg-brand-strong'

function Chip({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'danger' | 'brand' }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium leading-none',
        tone === 'danger' && 'bg-danger-soft text-danger',
        tone === 'brand' && 'bg-brand-soft text-brand',
        tone === 'neutral' && 'bg-surface-2 text-text-muted',
      )}
    >
      {children}
    </span>
  )
}

function CardShell({
  item,
  now,
  icon,
  chips,
  children,
  footer,
  testId,
}: {
  item: NeedsYouItem
  now: string
  icon: ReactNode
  chips?: ReactNode
  children: ReactNode
  footer: ReactNode
  testId: string
}) {
  return (
    <article data-testid={testId} className="rounded-lg border border-border bg-surface">
      <header className="flex items-center gap-2 px-4 pt-3.5 text-[13px]">
        <span className="text-text-muted">{icon}</span>
        <Link
          to="/ticket/$key"
          params={{ key: item.ticket }}
          className="rounded font-mono text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
        >
          {item.ticket}
        </Link>
        <span className="min-w-0 flex-1 truncate text-text-muted">{item.title}</span>
        {chips}
        <span className="shrink-0 text-xs tabular-nums text-text-faint">{ago(item.since, now)}</span>
      </header>
      <div className="space-y-3 px-4 py-3">{children}</div>
      <footer className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-2.5">{footer}</footer>
    </article>
  )
}

function CopyHash({ hash }: { hash: string }) {
  const [done, setDone] = useState(false)
  return (
    <span className="inline-flex items-center gap-1">
      <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-text-muted" title={hash}>
        {shortHash(hash)}
      </code>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label="Copy hash"
        onClick={() => {
          try {
            void navigator.clipboard?.writeText(hash)
          } catch {
            /* clipboard unavailable */
          }
          setDone(true)
          toast.success('Hash copied')
          setTimeout(() => setDone(false), 1500)
        }}
      >
        {done ? <Check /> : <Copy />}
      </Button>
    </span>
  )
}

function NoteDialog({
  open,
  onOpenChange,
  title,
  note,
  setNote,
  confirm,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  title: string
  note: string
  setNote: (s: string) => void
  confirm: string
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Say what should change. The note is required and goes to whoever has the ticket.</DialogDescription>
        </DialogHeader>
        <Textarea aria-label="Note" rows={4} value={note} onChange={(e) => setNote(e.target.value)} placeholder="What should change?" />
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!note.trim()} className={PRIMARY} onClick={onConfirm}>
            {confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------ question

export function QuestionCard({ item, ticket, dir, now, readOnly }: CommonProps & { item: NeedsYouItem; ticket?: TicketDocument }) {
  const act = useAct()
  const q = ticket?.questions_state.find((x) => x.id === item.ref)
  const id = `question:${item.ticket}:${item.ref}`
  const answer = (option: string, label: string) =>
    act(id, () => api.postAction(item.ticket, { action: 'answer', question: item.ref!, option }), {
      toast: `Answered ${item.ref} on ${item.ticket}: ${label}`,
      note: { text: `Answered ${item.ref} · ${label}`, detail: `${item.ticket} · undo isn't possible, it's signed` },
    })
  return (
    <CardShell
      testId={`card-${id}`}
      item={item}
      now={now}
      icon={<HelpCircle className="size-4" />}
      chips={item.blocking ? <Chip tone="danger">blocking</Chip> : undefined}
      footer={
        <>
          {q?.options?.map((o) => {
            const rec = q.recommended === o.key
            return (
              <Button
                key={o.key}
                size="sm"
                variant={rec ? 'default' : 'outline'}
                className={cn(rec && PRIMARY)}
                disabled={readOnly}
                onClick={() => void answer(o.key, o.label)}
              >
                {o.label}
                {rec && <span className="text-[11px] font-normal opacity-80">★ recommended</span>}
              </Button>
            )
          })}
          {q && !q.options?.length && (
            <Button asChild size="sm" variant="outline">
              <Link to="/ticket/$key" params={{ key: item.ticket }}>
                Open ticket to answer
              </Link>
            </Button>
          )}
          {q && <span className="ml-auto text-xs text-text-faint">Asked by {displayName(dir, q.asked_by)}</span>}
        </>
      }
    >
      <p className="text-[15px] font-medium leading-snug text-text">{item.text}</p>
      {q?.why && <p className="text-[13px] leading-relaxed text-text-muted">{q.why}</p>}
      {q?.options?.some((o) => o.cost) && (
        <ul className="space-y-0.5 text-xs text-text-muted">
          {q.options
            .filter((o) => o.cost)
            .map((o) => (
              <li key={o.key}>
                <span className="text-text">{o.label}</span> · {o.cost}
              </li>
            ))}
        </ul>
      )}
    </CardShell>
  )
}

// ------------------------------------------------------------------ approval (requirements / plan)

export function ApprovalCard({ item, ticket, now, readOnly }: CommonProps & { item: NeedsYouItem; ticket?: TicketDocument }) {
  const act = useAct()
  const gate = item.ref as GateName
  const [review, setReview] = useState(false)
  const [changes, setChanges] = useState(false)
  const [note, setNote] = useState('')
  const [presence, setPresence] = useState(false)
  const id = `approval:${item.ticket}:${gate}`
  const text = ticket?.body[gate === 'plan' ? 'plan' : 'requirements'] ?? ''
  const covers =
    gate === 'requirements'
      ? `Requirements · ${ticket?.acceptance.length ?? 0} acceptance criteria`
      : `Plan · ${ticket?.tasks.length ?? 0} tasks · ${ticket?.acceptance.length ?? 0} acceptance criteria`
  const hash = item.hash ?? ticket?.gates[gate].hash ?? ''

  const approve = async () => {
    setPresence(true)
    await new Promise((r) => setTimeout(r, 600))
    setPresence(false)
    setReview(false)
    await act(id, () => api.postAction(item.ticket, { action: 'approve', gate }), {
      toast: `Approved the ${gate} of ${item.ticket}`,
      note: { text: `Approved ${gate}`, detail: `${item.ticket} · signed with Touch ID` },
    })
  }
  const requestChanges = async () => {
    setChanges(false)
    await act(id, () => api.postAction(item.ticket, { action: 'request_changes', gate, text: note.trim() }), {
      toast: `Requested changes on the ${gate} of ${item.ticket}`,
      note: { text: `Requested changes · ${gate}`, detail: item.ticket },
    })
  }

  return (
    <CardShell
      testId={`card-${id}`}
      item={item}
      now={now}
      icon={<ShieldCheck className="size-4" />}
      chips={<Chip tone="brand">approve {gate}</Chip>}
      footer={
        <>
          <Button size="sm" className={PRIMARY} disabled={readOnly || !ticket} onClick={() => setReview(true)}>
            Review and approve
          </Button>
          <Button size="sm" variant="outline" disabled={readOnly || !ticket} onClick={() => setChanges(true)}>
            Request changes
          </Button>
        </>
      }
    >
      <p className="text-[15px] font-medium leading-snug text-text">{item.text}</p>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
        <span>{covers}</span>
        {hash && <CopyHash hash={hash} />}
      </div>

      <Dialog open={review} onOpenChange={(o) => !presence && setReview(o)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              Approve {gate} · {item.ticket}
            </DialogTitle>
            <DialogDescription>{item.title}. Your approval signs exactly this text.</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-3 text-[13px] leading-relaxed text-text">
            {text || 'No text.'}
          </div>
          {gate === 'requirements' && ticket && (
            <ul className="space-y-1 text-[13px] text-text-muted">
              {ticket.acceptance.map((a) => (
                <li key={a.id}>
                  <span className="mr-2 font-mono text-xs text-text-faint">{a.id}</span>
                  {a.text}
                </li>
              ))}
            </ul>
          )}
          {gate === 'plan' && ticket && (
            <ul className="space-y-1 text-[13px] text-text-muted">
              {ticket.tasks.map((t) => (
                <li key={t.id}>
                  <span className="mr-2 font-mono text-xs text-text-faint">{t.id}</span>
                  {t.text}
                </li>
              ))}
            </ul>
          )}
          <div className="flex items-center gap-2 text-xs text-text-muted">
            Hash <CopyHash hash={hash} />
          </div>
          <DialogFooter>
            <Button variant="ghost" disabled={presence} onClick={() => setReview(false)}>
              Cancel
            </Button>
            <Button className={PRIMARY} disabled={presence} onClick={() => void approve()}>
              {presence ? <Loader2 className="animate-spin" /> : <Fingerprint />}
              {presence ? 'Waiting for Touch ID…' : 'Approve · signs with Touch ID'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <NoteDialog
        open={changes}
        onOpenChange={setChanges}
        title={`Request changes · ${item.ticket}`}
        note={note}
        setNote={setNote}
        confirm="Request changes"
        onConfirm={() => void requestChanges()}
      />
    </CardShell>
  )
}

// ------------------------------------------------------------------ verdict

export function VerdictCard({ item, ticket, now, readOnly }: CommonProps & { item: NeedsYouItem; ticket?: TicketDocument }) {
  const act = useAct()
  const [changes, setChanges] = useState(false)
  const [note, setNote] = useState('')
  const id = `verdict:${item.ticket}`
  const ac = ticket?.acceptance_state ?? []
  const proven = ac.filter((a) => a.state === 'proven').length
  const receipts = ticket?.tasks_state.filter((t) => t.receipt).length ?? 0
  const give = (result: 'pass' | 'fail') =>
    act(id, () => api.postAction(item.ticket, { action: 'verdict', result, text: result === 'fail' ? note.trim() : undefined }), {
      toast: result === 'pass' ? `${item.ticket} is done` : `Sent ${item.ticket} back with changes`,
      note: { text: result === 'pass' ? 'Verdict · done' : 'Verdict · changes requested', detail: `${item.ticket} · signed` },
    })
  return (
    <CardShell
      testId={`card-${id}`}
      item={item}
      now={now}
      icon={<FileSearch className="size-4" />}
      chips={<Chip tone="brand">verdict</Chip>}
      footer={
        <>
          <Button size="sm" className={PRIMARY} disabled={readOnly || !ticket} onClick={() => void give('pass')}>
            Done
          </Button>
          <Button size="sm" variant="outline" disabled={readOnly || !ticket} onClick={() => setChanges(true)}>
            Changes
          </Button>
          <Button asChild size="sm" variant="ghost" className="ml-auto">
            <Link to="/ticket/$key" params={{ key: item.ticket }}>
              Open evidence
            </Link>
          </Button>
        </>
      }
    >
      <p className="text-[15px] font-medium leading-snug text-text">{item.text}</p>
      <p className="text-xs tabular-nums text-text-muted">
        <span className={cn(proven === ac.length && ac.length > 0 ? 'text-success' : 'text-text')}>
          AC {proven}/{ac.length} evidenced
        </span>
        {' · '}
        {receipts} receipts · {ticket?.artifacts.length ?? 0} artifacts
      </p>
      <NoteDialog
        open={changes}
        onOpenChange={setChanges}
        title={`Send back · ${item.ticket}`}
        note={note}
        setNote={setNote}
        confirm="Send back"
        onConfirm={() => {
          setChanges(false)
          void give('fail')
        }}
      />
    </CardShell>
  )
}

// ------------------------------------------------------------------ addon decision (rendered by core)

export function AddonDecisionCard({ d, readOnly }: { d: AddonDecision; readOnly: boolean }) {
  const act = useAct()
  const id = `addon:${d.id}`
  return (
    <div data-testid={`card-${id}`}>
      <AddonFrame addon={d.addon} title={d.title} slot="decision">
        <div className="space-y-3">
          <p className="text-[15px] font-medium leading-snug text-text">{d.question}</p>
          {d.detail && <p className="text-[13px] leading-relaxed text-text-muted">{d.detail}</p>}
          <div className="flex flex-wrap items-center gap-2">
            {d.options.map((o) => (
              <Button
                key={o.key}
                size="sm"
                variant={o.primary ? 'default' : 'outline'}
                className={cn(o.primary && PRIMARY)}
                disabled={readOnly}
                onClick={() =>
                  void act(id, () => api.runAddonAction(d.addon, d.action, { option: o.key, id: d.id, ticket: d.ticket }), {
                    toast: `${d.title}: ${o.label}`,
                    note: { text: `${d.title} · ${o.label}`, detail: `${d.addon}${d.ticket ? ` · ${d.ticket}` : ''} · signed by orch` },
                  })
                }
              >
                {o.label}
              </Button>
            ))}
            {d.ticket && (
              <Link
                to="/ticket/$key"
                params={{ key: d.ticket }}
                className="ml-auto rounded font-mono text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
              >
                {d.ticket}
              </Link>
            )}
          </div>
          <p className="border-t border-addon-border/60 pt-2 text-xs text-text-faint">
            requested by addon <span className="font-mono">{d.addon}</span> · confirmed and signed by orch
          </p>
        </div>
      </AddonFrame>
    </div>
  )
}
