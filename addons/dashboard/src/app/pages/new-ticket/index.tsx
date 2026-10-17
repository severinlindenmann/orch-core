import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { Lock, TriangleAlert, X } from 'lucide-react'
import { RadioGroup as RadioGroupPrimitive } from 'radix-ui'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { handoffMode } from '@/api/features'
import { MAX_SECTION_BYTES, type FollowUpAction, type HandoffAction, type HandoffRequest, type HandoffState } from '@/api/handoff'
import { can, roleOf } from '@/api/permissions'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { SECTIONS_BY_TYPE, requiredAtCreation, sectionLabel, type SectionName } from '@/api/sections'
import { ApiError, type BodySections, type Me, type NewTicketRequest, type Priority, type Size, type TicketDocument, type TicketType, type Visibility, type Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { PRIORITY_RANK, TYPE_ICON } from '../board/lib'
import { PeoplePicker, fieldCls, type PeopleValue } from './PeoplePicker'
import { SectionsEditor } from './SectionsEditor'
import { CompleteDetails } from './CompleteDetails'
import { queries } from '@/api/queries'
import { useMe } from '@/app/useMe'

const TYPES: TicketType[] = ['feature', 'bug', 'chore', 'spike', 'epic']
const PRIORITIES = (Object.keys(PRIORITY_RANK) as Priority[]).sort((a, b) => PRIORITY_RANK[b] - PRIORITY_RANK[a])
const SIZES: Size[] = ['xs', 's', 'm', 'l', 'xl']
/**
 * A new ticket through the terminal hand-off is one signed event with the title and type (host: key, type, title,
 * owner). What `new` cannot carry follows as further hand-offs ("Complete details", host contract 0.8.1 item 3): one
 * `set` for priority, size, labels and the parent epic, and one `section.set` per written section. What the host has no
 * person op for is said instead of dropped: restricted visibility, people, a due date and acceptance criteria.
 */
/**
 * The key "Complete details" may target: the one of the `new` request THIS dialog prepared and watched, and no other.
 * The state must be that request's own (same id, op `new`); anything else gives no key and no follow-up.
 */
/** The ticket exists but a follow-up signature was cancelled: say what is missing, never "not saved as a ticket". */
export function unsavedCopy(ticketKey: string, steps: FollowUpAction[]): string {
  const text = steps.some((s) => s.action === 'section')
  return `${ticketKey} was created; ${text ? 'its written text' : 'its details'} ${text ? 'is' : 'are'} not saved yet.`
}

export function ownKey(state: HandoffState | null, request: HandoffRequest | null): string | null {
  return state && request && state.id === request.id && request.op === 'new' && state.op === 'new' ? state.key : null
}

export function handoffNewTicket(d: Draft): { action: HandoffAction; details: FollowUpAction[] } | { problem: string } {
  const written = (Object.entries(d.sections) as [SectionName, string | undefined][]).filter(([n, t]) => SECTIONS_BY_TYPE[d.type][n] !== 'absent' && t?.trim())
  const later = [
    d.visibility !== 'workspace' && 'restricted visibility',
    (d.people.owner || d.people.assignees.length || d.people.reviewers.length) && 'people',
    d.due && 'a due date',
    d.acceptance.length > 0 && 'acceptance criteria',
  ].filter(Boolean)
  const big = written.find(([, t]) => new TextEncoder().encode((t as string).trim()).length > MAX_SECTION_BYTES)
  if (big) return { problem: `${sectionLabel(d.type, big[0])} is longer than ${MAX_SECTION_BYTES / 1024} KiB (counted in UTF-8 bytes). Shorten it before the ticket is created.` }
  if (later.length) return { problem: `A signed new ticket cannot carry ${later.join(', ')} yet: the host has no signed operation for it. Create the ticket first, then add these.` }
  const fields = { ...(d.priority !== 'medium' ? { priority: d.priority } : {}), ...(d.size ? { size: d.size } : {}), ...(d.labels.length ? { labels: d.labels } : {}), ...(d.parent && d.type !== 'epic' ? { parent: d.parent } : {}) }
  const details: FollowUpAction[] = Object.keys(fields).length ? [{ action: 'details', key: '', ...fields }] : []
  for (const [name, text] of written) {
    // A spike keeps its findings where other types have verification (the form's label says "Findings").
    const section = d.type === 'spike' && name === 'verification' ? 'findings' : name
    details.push({ action: 'section', key: '', section, text: (text as string).trim() })
  }
  return { action: { action: 'new', title: d.title.trim(), type: d.type }, details }
}

const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

export interface Draft {
  type: TicketType
  title: string
  priority: Priority
  size: Size | null
  labels: string[]
  parent: string | null
  due: string
  people: PeopleValue
  visibility: Visibility
  sections: BodySections
  acceptance: string[]
}

export const EMPTY: Draft = {
  type: 'feature',
  title: '',
  priority: 'medium',
  size: null,
  labels: [],
  parent: null,
  due: '',
  people: { owner: null, assignees: [], reviewers: [] },
  visibility: 'workspace',
  sections: {},
  acceptance: [],
}

/** Unsaved text: what the draft bar and the leave guard care about. */
const hasText = (d: Draft) => !!d.title.trim() || d.acceptance.length > 0 || Object.values(d.sections).some((s) => s?.trim())

export const draftKey = (person: string, ws: string) => `orch.dashboard.new-ticket.draft.${person}.${ws}`

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const isStrings = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === 'string')
const isNullableString = (v: unknown) => v === null || typeof v === 'string'

/** A stored draft, field by field over EMPTY; null when any stored field has the wrong shape (an old or foreign draft). */
function parseDraft(raw: unknown): Draft | null {
  if (!isObj(raw)) return null
  const d = { ...EMPTY, ...raw } as Record<keyof Draft, unknown>
  const p = d.people
  const v = d.visibility
  const ok =
    TYPES.includes(d.type as TicketType) &&
    typeof d.title === 'string' &&
    PRIORITIES.includes(d.priority as Priority) &&
    (d.size === null || SIZES.includes(d.size as Size)) &&
    isStrings(d.labels) &&
    isNullableString(d.parent) &&
    typeof d.due === 'string' &&
    isObj(p) && isNullableString(p.owner) && isStrings(p.assignees) && isStrings(p.reviewers) &&
    (v === 'workspace' || (isObj(v) && isStrings(v.restricted))) &&
    isObj(d.sections) && Object.values(d.sections).every((x) => x === undefined || typeof x === 'string') &&
    isStrings(d.acceptance)
  return ok ? (d as Draft) : null
}

/** The stored draft; a malformed one is removed and treated as no draft. */
export function readDraft(key: string): Draft | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const draft = parseDraft(JSON.parse(raw))
    if (!draft) localStorage.removeItem(key)
    return draft
  } catch {
    try {
      localStorage.removeItem(key)
    } catch {
      /* storage unavailable */
    }
    return null
  }
}
export function writeDraft(key: string, d: Draft | null) {
  try {
    if (d && hasText(d)) localStorage.setItem(key, JSON.stringify(d))
    else localStorage.removeItem(key)
  } catch {
    /* storage unavailable: the draft just is not kept */
  }
}

function problems(d: Draft): { title: string | null; sections: Partial<Record<SectionName, string>>; count: number } {
  const t = d.title.trim()
  const title = t.length < 3 || t.length > 120 ? 'The title needs 3 to 120 characters.' : null
  const sections: Partial<Record<SectionName, string>> = {}
  for (const n of requiredAtCreation(d.type)) if (!d.sections[n]?.trim()) sections[n] = `${sectionLabel(d.type, n)} ${n === 'summary' ? 'is' : 'are'} needed before the ticket can be created.`
  return { title, sections, count: (title ? 1 : 0) + Object.keys(sections).length }
}

function TypeControl({ value, onChange }: { value: TicketType; onChange: (t: TicketType) => void }) {
  return (
    <RadioGroupPrimitive.Root value={value} onValueChange={(v) => onChange(v as TicketType)} aria-label="Ticket type" orientation="horizontal" className="inline-flex gap-1 rounded-lg border border-border bg-surface p-1">
      {TYPES.map((t) => (
        <RadioGroupPrimitive.Item
          key={t}
          value={t}
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-3 text-[13px] capitalize text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand data-[state=checked]:bg-surface-3 data-[state=checked]:text-text"
        >
          {(() => {
            const Icon = TYPE_ICON[t]
            return <Icon aria-hidden className="size-3.5" />
          })()}
          {t}
        </RadioGroupPrimitive.Item>
      ))}
    </RadioGroupPrimitive.Root>
  )
}

function LabelsField({ value, onChange, existing }: { value: string[]; onChange: (v: string[]) => void; existing: string[] }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const typed = q.trim().toLowerCase()
  const add = (l: string) => {
    if (!value.includes(l)) onChange([...value, l])
    setQ('')
    setOpen(false)
  }
  return (
    <div className="space-y-1.5">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="w-full justify-start font-normal text-text-muted" aria-label="Add label">
            Add label…
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[240px] p-0" align="start">
          <Command>
            <CommandInput value={q} onValueChange={setQ} placeholder="Find or create a label" />
            <CommandList>
              <CommandEmpty>No label</CommandEmpty>
              <CommandGroup>
                {existing
                  .filter((l) => !value.includes(l))
                  .map((l) => (
                    <CommandItem key={l} value={l} onSelect={() => add(l)}>
                      {l}
                    </CommandItem>
                  ))}
                {typed && !existing.includes(typed) && !value.includes(typed) && (
                  <CommandItem value={`new-${typed}`} onSelect={() => add(typed)}>
                    Create “{typed}”
                  </CommandItem>
                )}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {value.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((l) => (
            <li key={l} className="inline-flex h-6 items-center gap-1 rounded-md border border-border bg-surface-2 pl-2 pr-1 text-[12px]">
              {l}
              <button type="button" aria-label={`Remove label ${l}`} className="rounded p-0.5 text-text-muted hover:text-text" onClick={() => onChange(value.filter((x) => x !== l))}>
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const Field = ({ label, htmlFor, children }: { label: string; htmlFor?: string; children: React.ReactNode }) => (
  <div className="space-y-1">
    <label htmlFor={htmlFor} className="text-[12px] text-text-muted">
      {label}
    </label>
    {children}
  </div>
)

/**
 * The page carries the overlay's draft (Open full page) without the "Draft restored" note:
 * set right before that navigation, read once by the page's form.
 */
let carriedFromOverlay = false
export const carryDraftToPage = () => void (carriedFromOverlay = true)


export interface NewTicketFormProps {
  me: Me
  workspace: Workspace
  /** `page` is the /tickets/new route; `overlay` is the same form in the New ticket sheet. */
  variant?: 'page' | 'overlay'
  /** Overlay: what is typed changed between nothing and something (the sheet asks before closing). */
  onDirtyChange?: (dirty: boolean) => void
  /** Overlay: a ticket was created with Create (the sheet closes and says so). */
  onCreated?: (ticket: TicketDocument) => void
  /** Overlay: the ticket exists (signed hand-off): close the sheet without asking about unsaved changes. */
  onClosed?: () => void
  /** Overlay: Cancel. */
  onCancel?: () => void
  /** Overlay: extra buttons in the footer (Open full page). */
  footerExtra?: ReactNode
}

export function NewTicketForm({ me, workspace, variant = 'page', onDirtyChange, onCreated, onClosed, onCancel, footerExtra }: NewTicketFormProps) {
  const overlay = variant === 'overlay'
  const navigate = useNavigate()
  const qc = useQueryClient()
  const key = draftKey(me.person, workspace.id)
  const restored = useMemo(() => readDraft(key), [key])
  const [draft, setDraft] = useState<Draft>(restored ?? EMPTY)
  // Read in the initializer (twice under StrictMode, so it is cleared in an effect, not here).
  const [showRestored, setShowRestored] = useState(() => !(variant === 'page' && carriedFromOverlay) && !!restored && hasText(restored))
  useEffect(() => {
    if (variant === 'page') carriedFromOverlay = false
  }, [variant])
  const root = useRef<HTMLDivElement>(null)
  const [attempted, setAttempted] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const leaving = useRef(false)
  const titleRef = useRef<HTMLInputElement>(null)
  const [refused, setRefused] = useState(0)
  const canCreate = can(roleOf(workspace, me.person), 'ticket.create')
  const dirty = hasText(draft)
  const check = problems(draft)
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange])

  const patch = useCallback((p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p })), [])
  useEffect(() => writeDraft(key, draft), [key, draft])

  const { data: all = [] } = useQuery(queries.ticketsAll(workspace.id))
  const labels = useMemo(() => [...new Set(all.flatMap((t) => t.labels))].sort(), [all])
  const epics = all.filter((t) => t.type === 'epic')

  // The page guards its own route; the overlay's sheet guards for the overlay (it also counts the quick ticket line).
  const blocker = useBlocker({ shouldBlockFn: () => !overlay && dirty && !leaving.current, withResolver: true, enableBeforeUnload: () => !overlay && dirty && !leaving.current })

  const [handoffNew, setHandoffNew] = useState<{ action: HandoffAction; details: FollowUpAction[]; another: boolean } | null>(null)
  const [followUp, setFollowUp] = useState<{ key: string; steps: FollowUpAction[]; another: boolean } | null>(null)
  /** A follow-up signature was cancelled: the ticket exists, these steps are not saved yet. */
  const [unsaved, setUnsaved] = useState<{ key: string; steps: FollowUpAction[]; another: boolean } | null>(null)
  const handoffOn = handoffMode(workspace.id)

  const create = useMutation({
    mutationFn: (req: NewTicketRequest) => api.createTicket(workspace.id, req),
    onError: (e) => setServerError(e instanceof ApiError ? e.message : 'Could not create the ticket.'),
  })

  const submitting = useRef(false)
  const submit = async (another: boolean) => {
    if (submitting.current) return
    setAttempted(true)
    setServerError(null)
    if (!canCreate) return
    if (check.count) {
      setRefused((n) => n + 1) // the effect below moves the focus to the first field that explains itself
      return
    }
    if (handoffOn) {
      // On a real host the ticket is created by a signed event: prepare it and sign in the terminal (design 4.5).
      const made = handoffNewTicket(draft)
      if ('problem' in made) return setServerError(made.problem)
      return setHandoffNew({ action: made.action, details: made.details, another })
    }
    submitting.current = true
    const needs = SECTIONS_BY_TYPE[draft.type]
    const sections: BodySections = {}
    for (const [n, text] of Object.entries(draft.sections) as [SectionName, string | undefined][]) if (needs[n] !== 'absent' && text?.trim()) sections[n] = text.trim()
    const res = await create
      .mutateAsync({
        type: draft.type,
        title: draft.title.trim(),
        priority: draft.priority,
        size: draft.size,
        labels: draft.labels,
        parent: draft.parent,
        due: draft.due || null,
        visibility: draft.visibility,
        people: draft.people,
        sections,
        acceptance: draft.acceptance,
      })
      .catch(() => null)
      .finally(() => {
        submitting.current = false
      })
    if (!res) return
    const { ticket } = res
    void qc.invalidateQueries()
    writeDraft(key, null)
    setShowRestored(false)
    if (another) {
      setDraft({ ...EMPTY, type: draft.type, priority: draft.priority })
      setAttempted(false)
      toast.success(`Created ${ticket.key}`)
      titleRef.current?.focus()
    } else if (overlay) {
      setDraft(EMPTY)
      onCreated?.(ticket)
    } else {
      leaving.current = true
      void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
    }
  }

  /** The signer's event is in the log: the ticket exists. */
  const handoffDone = (another: boolean, ticketKey: string | null, details: FollowUpAction[] = []) => {
    void qc.invalidateQueries()
    writeDraft(key, null)
    setShowRestored(false)
    setHandoffNew(null)
    // The ticket exists: nothing typed is unsaved any more. Clean at once (the sheet reads this synchronously, before
    // the next render), so no "Discard unsaved changes?" can appear while the follow-up signature or the close runs.
    setDraft(EMPTY)
    onDirtyChange?.(false)
    if (details.length && ticketKey) {
      // The new ticket exists; what `new` could not carry is further signatures, offered not forced.
      setFollowUp({ key: ticketKey, steps: details, another })
      return
    }
    afterCreate(another, ticketKey)
  }
  const afterCreate = (another: boolean, ticketKey: string | null) => {
    if (another) {
      setDraft({ ...EMPTY, type: draft.type, priority: draft.priority })
      setAttempted(false)
      toast.success(`Created ${ticketKey ?? 'the ticket'}`)
      titleRef.current?.focus()
      return
    }
    leaving.current = true
    setDraft(EMPTY)
    if (overlay) (onClosed ?? onCancel)?.()
    if (ticketKey) void navigate({ to: '/ticket/$key', params: { key: ticketKey } })
  }
  // A refused submit: focus the first invalid field (title, then the sections in page order).
  useEffect(() => {
    if (refused) root.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [refused])

  const discardAll = () => {
    writeDraft(key, null)
    setDraft(EMPTY)
    setShowRestored(false)
    setAttempted(false)
  }

  const summary = attempted && check.count ? `${check.count === 1 ? 'One thing needs' : `${check.count} things need`} your attention before the ticket can be created.` : null

  return (
    <div
      ref={root}
      className={overlay ? undefined : 'mx-auto max-w-[1100px]'}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          e.preventDefault()
          void submit(false)
        }
      }}
    >
      {!overlay && <h1 className="mb-4 text-xl font-semibold">New ticket</h1>}
      {!canCreate && (
        <div role="alert" className="mb-4 flex items-start gap-2 rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">
          <Lock className="mt-0.5 size-4 shrink-0" />
          <span>Viewers cannot create tickets in {workspace.name}. Ask an owner or maintainer.</span>
        </div>
      )}
      {showRestored && (
        <div role="status" className="mb-4 flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-[13px]">
          <span className="flex-1">Draft restored · from your last visit</span>
          <Button type="button" variant="ghost" size="sm" onClick={discardAll}>
            Discard
          </Button>
        </div>
      )}
      <TypeControl value={draft.type} onChange={(type) => patch({ type, parent: type === 'epic' ? null : draft.parent })} />
      <div className={overlay ? 'mt-5 grid grid-cols-[minmax(0,1fr)_240px] gap-6 pb-6' : 'mt-5 grid gap-8 pb-24 lg:grid-cols-[minmax(0,1fr)_280px]'}>
        <div className="space-y-5">
          <div className="space-y-1.5">
            <label htmlFor="nt-title" className="text-[13px] font-medium">
              Title
            </label>
            <Input
              id="nt-title"
              ref={titleRef}
              autoFocus={!overlay}
              value={draft.title}
              onChange={(e) => patch({ title: e.target.value })}
              maxLength={160}
              placeholder="What needs to happen?"
              aria-invalid={(attempted && !!check.title) || undefined}
              aria-describedby={attempted && check.title ? 'nt-title-error' : undefined}
              className="h-11 text-lg md:text-lg"
            />
            {attempted && check.title && (
              <p id="nt-title-error" className="text-[12px] text-danger">
                {check.title}
              </p>
            )}
          </div>
          <SectionsEditor
            type={draft.type}
            sections={draft.sections}
            onSection={(n, text) => setDraft((d) => ({ ...d, sections: { ...d.sections, [n]: text } }))}
            acceptance={draft.acceptance}
            onAcceptance={(acceptance) => patch({ acceptance })}
            invalid={attempted ? check.sections : {}}
            collapsible={overlay}
          />
        </div>
        <aside className="space-y-4" aria-label="Properties">
          <Field label="Priority" htmlFor="nt-priority">
            <select id="nt-priority" className={fieldCls} value={draft.priority} onChange={(e) => patch({ priority: e.target.value as Priority })}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {p[0].toUpperCase() + p.slice(1)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Size" htmlFor="nt-size">
            <select id="nt-size" className={fieldCls} value={draft.size ?? ''} onChange={(e) => patch({ size: (e.target.value || null) as Size | null })}>
              <option value="">Not sized</option>
              {SIZES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Labels">
            <LabelsField value={draft.labels} onChange={(l) => patch({ labels: l })} existing={labels} />
          </Field>
          {draft.type !== 'epic' && (
            <Field label="Parent epic" htmlFor="nt-parent">
              <select id="nt-parent" className={fieldCls} value={draft.parent ?? ''} onChange={(e) => patch({ parent: e.target.value || null })}>
                <option value="">None</option>
                {epics.map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.key} · {t.title}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label="Due" htmlFor="nt-due">
            <Input id="nt-due" type="date" value={draft.due} onChange={(e) => patch({ due: e.target.value })} className="h-8 text-[13px]" />
          </Field>
          <PeoplePicker members={workspace.members} creator={me.person} people={draft.people} onPeople={(people) => patch({ people })} visibility={draft.visibility} onVisibility={(visibility) => patch({ visibility })} />
        </aside>
      </div>
      <div className={overlay ? 'sticky bottom-0 -mx-6 border-t border-border bg-surface px-6 py-3' : 'sticky bottom-0 -mx-4 mt-8 border-t border-border bg-bg px-4 py-3 @[60rem]/page:-mx-6 @[60rem]/page:px-6'}>
        {(summary || serverError) && (
          <div role="alert" className="mb-2 flex items-start gap-2 text-[13px] text-danger">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <ul>
              {[...(summary ? [summary] : []), ...(serverError ? [serverError] : [])].map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex items-center gap-2">
          <Button type="button" aria-label="Create" aria-keyshortcuts="Meta+Enter Control+Enter" onClick={() => void submit(false)} disabled={!canCreate || create.isPending || !!handoffNew}>
            Create
            <kbd className="ml-1 rounded border border-border/40 px-1 font-mono text-[10px] opacity-70">{isMac ? '⌘↵' : 'Ctrl ↵'}</kbd>
          </Button>
          <Button type="button" variant="outline" onClick={() => void submit(true)} disabled={!canCreate || create.isPending}>
            Create and open another
          </Button>
          {footerExtra}
          <Button type="button" variant="ghost" onClick={() => (overlay ? onCancel?.() : void navigate({ to: '/tickets' }))}>
            Cancel
          </Button>
        </div>
      </div>

      {handoffNew && (
        <HandoffDialog
          title="New ticket"
          ws={workspace.id}
          action={handoffNew.action}
          autoStart
          onClose={() => setHandoffNew(null)}
          onSettled={(phase, state, request) => phase === 'done' && handoffDone(handoffNew.another, ownKey(state, request), handoffNew.details)}
        />
      )}

      {followUp && (
        <CompleteDetails
          ws={workspace.id}
          ticketKey={followUp.key}
          steps={followUp.steps}
          onFinish={(done) => {
            const f = followUp
            setFollowUp(null)
            void qc.invalidateQueries()
            if (done < f.steps.length) return setUnsaved({ ...f, steps: f.steps.slice(done) })
            afterCreate(f.another, f.key)
          }}
        />
      )}

      {unsaved && (
        <Dialog open onOpenChange={(o) => !o && (setUnsaved(null), afterCreate(unsaved.another, unsaved.key))}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{unsaved.key} was created</DialogTitle>
              <DialogDescription data-testid="followup-unsaved">{unsavedCopy(unsaved.key, unsaved.steps)}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  const u = unsaved
                  setUnsaved(null)
                  afterCreate(u.another, u.key)
                }}
              >
                Open {unsaved.key}
              </Button>
              <Button
                onClick={() => {
                  const u = unsaved
                  setUnsaved(null)
                  setFollowUp(u)
                }}
              >
                Try again
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      <Dialog open={blocker.status === 'blocked'} onOpenChange={(o) => !o && blocker.reset?.()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard this draft?</DialogTitle>
            <DialogDescription>What you typed has not been saved as a ticket.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => blocker.reset?.()}>
              Keep editing
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                leaving.current = true
                writeDraft(key, null)
                blocker.proceed?.()
              }}
            >
              Discard draft
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export function NewTicketPage() {
  const { workspace } = useWorkspace()
  const { data: me } = useMe()
  usePageHeader('New ticket')
  if (!me || !workspace) return <Skeleton className="mx-auto h-96 max-w-[1100px]" aria-label="Loading" />
  return <NewTicketForm key={`${workspace.id}:${me.person}`} me={me} workspace={workspace} />
}
