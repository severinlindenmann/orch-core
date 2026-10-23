import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { Lock, TriangleAlert, X } from 'lucide-react'
import { RadioGroup as RadioGroupPrimitive } from 'radix-ui'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { handoffMode } from '@/api/features'
import { MAX_IMAGES, missingPasted, pastedIn } from '@/api/pasteImage'
import { MAX_CRITERIA, MAX_SECTION_BYTES, MAX_SEQUENCE_STEPS, type HandoffAction, type HandoffRequest, type HandoffState } from '@/api/handoff'
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
import { AgentLine, NotSignedBlock } from './AgentLine'
import { harnessName } from '@/components/agents/AgentChoice'
import type { StartPlan } from './useStartPlan'
import { StartAgentDialog, type StartInitial } from '@/app/terminal/real/StartAgentDialog'
import { cn } from '@/lib/utils'
import { newIdem } from '@/api/runs'
import { useStartAfterCreate } from './useStartAfterCreate'
import { useStartPlan } from './useStartPlan'
import type { Reviewed } from './startAfterCreate'
import { queries } from '@/api/queries'
import { useMe } from '@/app/useMe'

const TYPES: TicketType[] = ['feature', 'bug', 'chore', 'spike', 'epic']
const PRIORITIES = (Object.keys(PRIORITY_RANK) as Priority[]).sort((a, b) => PRIORITY_RANK[b] - PRIORITY_RANK[a])
const SIZES: Size[] = ['xs', 's', 'm', 'l', 'xl']
/**
 * A new ticket through the hand-off is ONE review and ONE confirmation (B11, finding 9): the title, the type, the
 * fields and every written section go to the signer as one intent. The signer signs `ticket.created`, the fields and one
 * `ticket.updated` per written section, each step checked against the intent; the review shows the ticket AND its text.
 * B12D (finding N3): acceptance criteria and people (owner, assignees, reviewers) are signed in the same review, as
 * `ac.add` and `people.set` steps after the text. What the host has no signed operation for is said instead of
 * dropped: restricted visibility and a due date.
 */
/**
 * The key of the ticket: the one of the `new` request THIS dialog prepared and watched, and no other.
 * The state must be that request's own (same id, op `new`); anything else gives no key.
 */
export function ownKey(state: HandoffState | null, request: HandoffRequest | null): string | null {
  return state && request && state.id === request.id && request.op === 'new' && state.op === 'new' ? state.key : null
}

/** The people the sequence signs: the creator is already the owner, so naming them again is no change. */
export function sequencePeople(p: PeopleValue, creator?: string): { owner?: string; assignees?: string[]; reviewers?: string[] } | undefined {
  const out: { owner?: string; assignees?: string[]; reviewers?: string[] } = {}
  if (p.owner && p.owner !== creator) out.owner = p.owner
  if (p.assignees.length) out.assignees = [...new Set(p.assignees)].sort()
  if (p.reviewers.length) out.reviewers = [...new Set(p.reviewers)].sort()
  return Object.keys(out).length ? out : undefined
}

/** The access would be one signature too many, so the ticket alone can be signed but not with the access (only the limit causes the refusal). */
export function accessTooMany(d: Draft, creator: string | undefined, hours: number): boolean {
  const withAccess = handoffNewTicket(d, creator, hours)
  return 'problem' in withAccess && !!withAccess.tooMany && !('problem' in handoffNewTicket(d, creator, null))
}

/**
 * `agentHours`: the person has no usable agent access, so it is signed as the LAST step of the same confirmation (one
 * more signature against the limit). `tooMany` marks a refusal that only the limit causes, so the form can fall back to
 * creating without the start instead of failing.
 */
export function handoffNewTicket(d: Draft, creator?: string, agentHours?: number | null): { action: HandoffAction } | { problem: string; tooMany?: true } {
  const written = (Object.entries(d.sections) as [SectionName, string | undefined][]).filter(([n, t]) => SECTIONS_BY_TYPE[d.type][n] !== 'absent' && t?.trim())
  const later = [d.visibility !== 'workspace' && 'restricted visibility', d.due && 'a due date'].filter(Boolean)
  const big = written.find(([, t]) => new TextEncoder().encode((t as string).trim()).length > MAX_SECTION_BYTES)
  if (big) return { problem: `${sectionLabel(d.type, big[0])} is longer than ${MAX_SECTION_BYTES / 1024} KiB (counted in UTF-8 bytes). Shorten it before the ticket is created.` }
  if (later.length) return { problem: `A signed new ticket cannot carry ${later.join(', ')} yet: the host has no signed operation for it. Create the ticket first, then add these.` }
  const criteria = d.acceptance.map((t) => t.trim()).filter(Boolean)
  if (criteria.length > MAX_CRITERIA) return { problem: `At most ${MAX_CRITERIA} acceptance criteria can be signed together with a new ticket. Add the rest after the ticket exists.` }
  // A spike keeps its findings where other types have verification (the form's label says "Findings").
  const sections = Object.fromEntries(written.map(([name, text]) => [d.type === 'spike' && name === 'verification' ? 'findings' : name, (text as string).trim()]))
  const people = sequencePeople(d.people, creator)
  // R3 part B: screenshots pasted into the sections are signed first (one signature each), from the bytes this tab holds
  const texts = Object.values(sections) as string[]
  if (missingPasted(texts).length) return { problem: 'A pasted picture is no longer here (the page was reloaded). Paste it again, or take its line out of the text.' }
  const images = pastedIn(texts)
  if (images.length > MAX_IMAGES) return { problem: `At most ${MAX_IMAGES} pictures fit in one signing run.` }
  const hasFields = d.priority !== 'medium' || !!d.size || d.labels.length > 0 || !!(d.parent && d.type !== 'epic')
  const steps = 1 + (hasFields ? 1 : 0) + written.length + criteria.length + (people ? Object.keys(people).length : 0) + images.length + (agentHours ? 1 : 0)
  if (steps > MAX_SEQUENCE_STEPS) return { tooMany: true, problem: `That is ${steps} separate changes and one signing run carries at most ${MAX_SEQUENCE_STEPS}. Leave some sections or criteria for later.` }
  return {
    action: {
      action: 'new_sequence',
      title: d.title.trim(),
      type: d.type,
      ...(d.priority !== 'medium' ? { priority: d.priority } : {}),
      ...(d.size ? { size: d.size } : {}),
      ...(d.labels.length ? { labels: d.labels } : {}),
      ...(d.parent && d.type !== 'epic' ? { parent: d.parent } : {}),
      sections,
      ...(criteria.length ? { acceptance: criteria } : {}),
      ...(people ? { people } : {}),
      ...(images.length ? { images } : {}),
      ...(agentHours ? { agent: { hours: agentHours } } : {}),
    } as HandoffAction,
  }
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

  // "Create and start agent": what the review shows about the start, kept with the request so the start afterwards is
  // compared with exactly that (never with whatever the form says by then). One idem per review.
  interface StartSnapshot { reviewed: Reviewed; idem: string; priorGrantIds: string[]; view: Pick<StartPlan, 'shown' | 'choices' | 'choice'>; withAccess: boolean }
  const [handoffNew, setHandoffNew] = useState<{ action: HandoffAction; another: boolean; start: StartSnapshot | null } | null>(null)
  const handoffOn = handoffMode(workspace.id)
  const plan = useStartPlan(workspace, me.person)
  const after = useStartAfterCreate()
  const partialKey = useRef<{ key: string; initial: StartInitial } | null>(null)
  const busyAfter = after.state.kind !== 'idle'
  // The access would be one signature too many: say so now, and create without starting (no surprise at the click).
  const tooMany = useMemo(() => {
    if (!plan.available || !plan.needsGrant) return false
    return accessTooMany(draft, me.person, plan.hours)
  }, [plan.available, plan.needsGrant, plan.hours, draft, me.person])
  const startPossible = plan.available && !tooMany
  const startFirst = startPossible && plan.pref.start

  const create = useMutation({
    mutationFn: (req: NewTicketRequest) => api.createTicket(workspace.id, req),
    onError: (e) => setServerError(e instanceof ApiError ? e.message : 'Could not create the ticket.'),
  })

  const submitting = useRef(false)
  // E-7: a criterion typed in the box but not added with "Add" is added when the ticket is submitted, never dropped.
  const flushAc = useRef<(() => string) | null>(null)
  const submit = async (another: boolean, wantStart = false) => {
    if (submitting.current) return
    const extra = flushAc.current?.() ?? ''
    const full = extra ? { ...draft, acceptance: [...draft.acceptance, extra] } : draft
    if (extra) patch({ acceptance: full.acceptance })
    setAttempted(true)
    setServerError(null)
    if (!canCreate) return
    if (check.count) {
      setRefused((n) => n + 1) // the effect below moves the focus to the first field that explains itself
      return
    }
    if (handoffOn) {
      // On a real host the ticket is created by a signed event: prepare it and sign in the terminal (design 4.5).
      // "Usable" is a matter of time: decide on the grant as it is right now, not as the form last drew it.
      const fresh = plan.grantNow()
      // F-712-B: so is the step limit (it depends on whether the access has to be signed too): recompute it with the fresh grant,
      // so a grant that lapsed since the form was drawn falls back to "Only create" and not to a form error.
      const startPossibleNow = plan.available && !(!fresh && accessTooMany(full, me.person, plan.hours))
      const startNow = wantStart && startPossibleNow && !another
      if (startPossibleNow) plan.setPref({ start: wantStart })
      const withAccess = startNow && !fresh
      const made = handoffNewTicket(full, me.person, withAccess ? plan.hours : null)
      if ('problem' in made) return setServerError(made.problem)
      const start: StartSnapshot | null = startNow
        ? { reviewed: { ...plan.reviewed, grantId: fresh?.id ?? null }, idem: newIdem(), priorGrantIds: plan.priorGrantIds, view: { shown: plan.shown, choices: plan.choices, choice: plan.choice }, withAccess }
        : null
      return setHandoffNew({ action: made.action, another, start })
    }
    submitting.current = true
    const needs = SECTIONS_BY_TYPE[full.type]
    const sections: BodySections = {}
    for (const [n, text] of Object.entries(full.sections) as [SectionName, string | undefined][]) if (needs[n] !== 'absent' && text?.trim()) sections[n] = text.trim()
    const res = await create
      .mutateAsync({
        type: full.type,
        title: full.title.trim(),
        priority: full.priority,
        size: full.size,
        labels: full.labels,
        parent: full.parent,
        due: full.due || null,
        visibility: full.visibility,
        people: full.people,
        sections,
        acceptance: full.acceptance,
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

  /** The ticket exists: nothing typed is unsaved any more. Clean at once (the sheet reads this synchronously, before
   * the next render), so no "Discard unsaved changes?" can appear while the follow-up signature or the close runs. */
  const ticketExists = () => {
    void qc.invalidateQueries()
    writeDraft(key, null)
    setShowRestored(false)
    setDraft(EMPTY)
    onDirtyChange?.(false)
  }
  /** The signer's event is in the log: the ticket exists. */
  const handoffDone = (another: boolean, ticketKey: string | null, start: StartSnapshot | null, deposit?: string) => {
    ticketExists()
    setHandoffNew(null)
    // One click signed the agent access too: only a confirmed hand-over to the runner counts as access given.
    if (start && ticketKey && start.withAccess && deposit !== undefined && deposit !== 'confirmed') return after.noAccess(ticketKey, initialOf(start))
    // Only the key of this form's own `new` request starts anything; without it the ticket is just created.
    if (start && ticketKey) return void beginStart(ticketKey, start)
    afterCreate(another, ticketKey)
  }
  const initialOf = (snap: StartSnapshot): StartInitial => ({ harness: snap.reviewed.choice.harness, mode: snap.reviewed.choice.mode, ...(snap.reviewed.choice.model ? { model: snap.reviewed.choice.model } : {}) })
  /** Every signature landed: preview for the ticket's own key and start only if it is what the review showed. */
  const beginStart = async (ticketKey: string, snap: StartSnapshot) => {
    const out = await after.run({ ws: workspace.id, key: ticketKey, reviewed: snap.reviewed, idem: snap.idem, priorGrantIds: snap.priorGrantIds })
    if (out.kind === 'started') {
      toast.success(`Created ${ticketKey} and started ${harnessName(snap.reviewed.choice.harness)}`)
      afterCreate(false, ticketKey)
    } else toast.warning(out.reason)
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

  const primaryLabel = startFirst ? 'Create and start agent' : startPossible || tooMany ? 'Only create' : handoffOn ? 'Review ticket' : 'Create'
  const summary = attempted && check.count ? `${check.count === 1 ? 'One thing needs' : `${check.count} things need`} your attention before the ticket can be created.` : null

  return (
    <div
      ref={root}
      // M-1: once the ticket exists and the Start agent dialog (or the "no access" notice) is open, the emptied form is not shown behind it.
      className={cn(overlay ? undefined : 'mx-auto max-w-[1100px]', (after.state.kind === 'ask' || after.state.kind === 'no-access') && 'hidden')}
      data-ticket-created={after.state.kind === 'ask' || after.state.kind === 'no-access' ? '' : undefined}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          e.preventDefault()
          void submit(false, startFirst)
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
            flushRef={flushAc}
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
        <AgentLine plan={plan} tooMany={tooMany} />
        {after.state.kind === 'running' && <p role="status" data-testid="agent-starting" className="mb-2 text-[13px] text-text-muted">Created {after.state.key}. Starting the agent…</p>}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" aria-label={primaryLabel} aria-keyshortcuts="Meta+Enter Control+Enter" onClick={() => void submit(false, startFirst)} disabled={!canCreate || create.isPending || !!handoffNew || busyAfter}>
            {primaryLabel}
            <kbd className="ml-1 rounded border border-border/40 px-1 font-mono text-[10px] opacity-70">{isMac ? '⌘↵' : 'Ctrl ↵'}</kbd>
          </Button>
          {startPossible && (
            <Button type="button" variant="outline" onClick={() => void submit(false, !startFirst)} disabled={!canCreate || create.isPending || !!handoffNew || busyAfter}>
              {startFirst ? 'Only create' : 'Create and start agent'}
            </Button>
          )}
          {!startFirst && (
            <Button type="button" variant="outline" onClick={() => void submit(true)} disabled={!canCreate || create.isPending || busyAfter}>
              Create and open another
            </Button>
          )}
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
          startsAgent={!!handoffNew.start}
          unsigned={handoffNew.start ? <NotSignedBlock plan={handoffNew.start.view} coreSaysSo={handoffNew.start.withAccess} /> : undefined}
          onClose={() => {
            // The ticket landed but its agent access did not: say so, and offer the Start agent dialog (which has the access card).
            const landed = partialKey.current
            partialKey.current = null
            setHandoffNew(null)
            if (landed) { ticketExists(); after.noAccess(landed.key, landed.initial) }
          }}
          onSettled={(phase, state, request) => {
            const ticketKey = ownKey(state, request)
            if (phase === 'done') return handoffDone(handoffNew.another, ticketKey, handoffNew.start, state?.deposit)
            const p = state?.progress
            if (handoffNew.start && ticketKey && p?.last === 'grant' && p.done === p.of - 1) {
              partialKey.current = { key: ticketKey, initial: initialOf(handoffNew.start) }
              ticketExists() // the ticket is there: nothing typed is unsaved, and a second Create would make a second ticket
            }
          }}
        />
      )}

      {after.state.kind === 'ask' && (
        <StartAgentDialog
          ws={workspace.id}
          ticket={after.state.key}
          open
          initial={after.state.initial}
          onStarted={() => toast.success('Agent started')}
          onClose={() => { const k = after.state.kind === 'ask' ? after.state.key : null; after.reset(); if (k) afterCreate(false, k) }}
        />
      )}
      {after.state.kind === 'no-access' && (
        <Dialog open onOpenChange={(o) => { if (!o) { const k = after.state.kind === 'no-access' ? after.state.key : null; after.reset(); if (k) afterCreate(false, k) } }}>
          <DialogContent data-testid="agent-no-access">
            <DialogHeader>
              <DialogTitle>Ticket created</DialogTitle>
              <DialogDescription>
                {after.state.key} exists. Agent access is not confirmed yet: the last signature has not arrived, so no agent was started. Start one now to give access, or open the ticket.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => { const k = after.state.kind === 'no-access' ? after.state.key : null; after.reset(); if (k) afterCreate(false, k) }}>Open the ticket</Button>
              <Button onClick={() => { if (after.state.kind === 'no-access') after.ask(after.state.key, after.state.initial) }}>Start agent</Button>
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
