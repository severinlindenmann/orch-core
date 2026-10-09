import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { Lock, TriangleAlert, X } from 'lucide-react'
import { RadioGroup as RadioGroupPrimitive } from 'radix-ui'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { can, roleOf } from '@/api/permissions'
import { SECTIONS_BY_TYPE, requiredAtCreation, sectionLabel, type SectionName } from '@/api/sections'
import { ApiError, type BodySections, type Me, type NewTicketRequest, type Priority, type Size, type TicketType, type Visibility, type Workspace } from '@/api/types'
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

const TYPES: TicketType[] = ['feature', 'bug', 'chore', 'spike', 'epic']
const PRIORITIES = (Object.keys(PRIORITY_RANK) as Priority[]).sort((a, b) => PRIORITY_RANK[b] - PRIORITY_RANK[a])
const SIZES: Size[] = ['xs', 's', 'm', 'l', 'xl']
const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

interface Draft {
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

const EMPTY: Draft = {
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

const draftKey = (person: string, ws: string) => `orch.dashboard.new-ticket.draft.${person}.${ws}`

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
function readDraft(key: string): Draft | null {
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
function writeDraft(key: string, d: Draft | null) {
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
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-3 text-[13px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand data-[state=checked]:bg-surface-3 data-[state=checked]:text-text"
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

function NewTicketForm({ me, workspace }: { me: Me; workspace: Workspace }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const key = draftKey(me.person, workspace.id)
  const restored = useMemo(() => readDraft(key), [key])
  const [draft, setDraft] = useState<Draft>(restored ?? EMPTY)
  const [showRestored, setShowRestored] = useState(!!restored && hasText(restored))
  const [attempted, setAttempted] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const leaving = useRef(false)
  const titleRef = useRef<HTMLInputElement>(null)
  const [refused, setRefused] = useState(0)
  const canCreate = can(roleOf(workspace, me.person), 'ticket.create')
  const dirty = hasText(draft)
  const check = problems(draft)

  usePageHeader('New ticket')

  const patch = useCallback((p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p })), [])
  useEffect(() => writeDraft(key, draft), [key, draft])

  const { data: all = [] } = useQuery({ queryKey: ['tickets', workspace.id, 'all'], queryFn: () => api.listTickets(workspace.id) })
  const labels = useMemo(() => [...new Set(all.flatMap((t) => t.labels))].sort(), [all])
  const epics = all.filter((t) => t.type === 'epic')

  const blocker = useBlocker({ shouldBlockFn: () => dirty && !leaving.current, withResolver: true, enableBeforeUnload: () => dirty && !leaving.current })

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
    } else {
      leaving.current = true
      void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
    }
  }

  // A refused submit: focus the first invalid field (title, then the sections in page order).
  useEffect(() => {
    if (refused) document.querySelector<HTMLElement>('main [aria-invalid="true"]')?.focus()
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
      className="mx-auto max-w-[1100px]"
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          e.preventDefault()
          void submit(false)
        }
      }}
    >
      <h1 className="mb-4 text-xl font-semibold">New ticket</h1>
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
      <div className="mt-5 grid gap-8 pb-24 lg:grid-cols-[minmax(0,1fr)_280px]">
        <div className="space-y-5">
          <div className="space-y-1.5">
            <label htmlFor="nt-title" className="text-[13px] font-medium">
              Title
            </label>
            <Input
              id="nt-title"
              ref={titleRef}
              autoFocus
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
          />
        </div>
        <aside className="space-y-4" aria-label="Properties">
          <Field label="Priority" htmlFor="nt-priority">
            <select id="nt-priority" className={fieldCls} value={draft.priority} onChange={(e) => patch({ priority: e.target.value as Priority })}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {p}
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
      <div className="sticky bottom-0 -mx-6 mt-8 border-t border-border bg-bg px-6 py-3">
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
          <Button type="button" aria-label="Create" aria-keyshortcuts="Meta+Enter Control+Enter" onClick={() => void submit(false)} disabled={!canCreate || create.isPending}>
            Create
            <kbd className="ml-1 rounded border border-border/40 px-1 font-mono text-[10px] opacity-70">{isMac ? '⌘↵' : 'Ctrl ↵'}</kbd>
          </Button>
          <Button type="button" variant="outline" onClick={() => void submit(true)} disabled={!canCreate || create.isPending}>
            Create and open another
          </Button>
          <Button type="button" variant="ghost" onClick={() => void navigate({ to: '/tickets' })}>
            Cancel
          </Button>
        </div>
      </div>

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
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  if (!me || !workspace) return <Skeleton className="mx-auto h-96 max-w-[1100px]" aria-label="Loading" />
  return <NewTicketForm key={`${workspace.id}:${me.person}`} me={me} workspace={workspace} />
}
