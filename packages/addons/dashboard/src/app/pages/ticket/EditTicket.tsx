// B12D (finding N3): edit an existing ticket's text, acceptance criteria, repositories and people, then ONE review.
//
// The page sends what changed plus the log head it showed (`head_seq`); the server reads the ticket, refuses the edit
// when the ticket has changed since, and the signer signs one ordinary event per change, each checked against the
// reviewed intent. Nothing here signs or names an op: it builds the typed `edit_sequence` action.
import { Pencil, Plus, X } from 'lucide-react'
import { createContext, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { handoffMode } from '@/api/features'
import { MAX_IMAGES, missingPasted, pastedIn } from '@/api/pasteImage'
import { can } from '@/api/permissions'
import { MAX_CRITERIA, MAX_SECTION_BYTES, MAX_SEQUENCE_STEPS, type HandoffAction } from '@/api/handoff'
import { SECTIONS_BY_TYPE, SECTION_ORDER, sectionLabel, type SectionName } from '@/api/sections'
import { branchProblem, MAX_LINK_REPOS, suggestedBranch } from '@/api/links'
import type { TicketDocument, TicketLinks } from '@/api/types'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { workspaceOfTicket } from '@/api/workspaces'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { Section } from '../new-ticket/SectionsEditor'
import { PeoplePicker, type PeopleValue } from '../new-ticket/PeoplePicker'
import type { Viewer } from './shared'

export type EditFocus = { kind: 'ac' | 'task'; id: string } | { kind: 'title' }
export const MAX_TITLE = 200
type TaskDraft = { text: string; verify: string; proves: string[] }
type TaskDrafts = { edit: Record<string, TaskDraft>; add: TaskDraft[] }

type Edit = NonNullable<Extract<HandoffAction, { action: 'edit_sequence' }>>

/** What the person changed, as the typed action; or what is wrong with it. Pure: tested without the dialog. */
export function buildEdit(
  ticket: { title?: string } & Pick<TicketDocument, 'key' | 'type' | 'body' | 'acceptance_state' | 'people' | 'head'> & { artifacts?: { name: string }[] } & { links?: Pick<TicketLinks, 'repos' | 'branches'>; tasks_state?: TicketDocument['tasks_state'] },
  want: { title?: string; tasks?: TaskDrafts; sections: Partial<Record<SectionName, string>>; criteria: Record<string, string>; added: string[]; people: PeopleValue; links?: { repos: string[]; branches: Record<string, string> } },
  mayEditPeople: boolean,
): { action: Edit; steps: number } | { problem: string } | { nothing: true } {
  // r7-P07: a new title is one line of 1-200 characters; the same as before is no change
  let title: string | undefined
  if (want.title !== undefined && ticket.title !== undefined) {
    const text = want.title.trim()
    if (text !== ticket.title.trim()) {
      if (!text) return { problem: 'The title cannot be empty. Write the new title, or leave it as it is.' }
      if (/[\r\n]/.test(text)) return { problem: 'The title is one line. Take the line break out.' }
      if ([...text].length > MAX_TITLE) return { problem: `The title is longer than ${MAX_TITLE} characters. Shorten it.` }
      title = text
    }
  }
  const sections: Record<string, string> = {}
  for (const name of SECTION_ORDER) {
    if (SECTIONS_BY_TYPE[ticket.type][name] === 'absent') continue
    const was = (ticket.body[name] ?? '').trim()
    const text = (want.sections[name] ?? was).trim() // a section the form never touched is unchanged
    if (text === was) continue
    if (!text) return { problem: `${sectionLabel(ticket.type, name)} cannot be emptied here. Write the new text, or leave it as it is.` }
    if (new TextEncoder().encode(text).length > MAX_SECTION_BYTES) return { problem: `${sectionLabel(ticket.type, name)} is longer than ${MAX_SECTION_BYTES / 1024} KiB (counted in UTF-8 bytes). Shorten it.` }
    // A spike keeps its findings where other types have verification (as the New ticket form does).
    sections[ticket.type === 'spike' && name === 'verification' ? 'findings' : name] = text
  }
  const edit: Record<string, string> = {}
  for (const a of ticket.acceptance_state) {
    const text = (want.criteria[a.id] ?? a.text).trim()
    if (text !== a.text.trim()) {
      if (!text) return { problem: `${a.id} cannot be emptied. Write the new text, or leave it as it is.` }
      edit[a.id] = text
    }
  }
  const add = want.added.map((t) => t.trim()).filter(Boolean)
  if (Object.keys(edit).length + add.length > MAX_CRITERIA) return { problem: `At most ${MAX_CRITERIA} acceptance criteria can be changed or added in one signature.` }
  const tasks: NonNullable<Edit['tasks']> = { edit: {}, add: [] }
  const sortProves = (ids: string[]) => [...new Set(ids)].sort((a, b) => Number(a.slice(2)) - Number(b.slice(2)))
  const taskProblem = (t: TaskDraft): string | undefined => {
    if (!t.text.trim()) return 'Task text cannot be empty.'
    if ([...t.text.trim()].length > 500) return 'Task text is too long. Use at most 500 characters.'
    if (new TextEncoder().encode(t.verify.trim()).length > 4096) return 'The check is too long. Shorten it.'
    if (t.proves.some(id => !ticket.acceptance_state.some(a => a.id === id))) return 'Choose criteria already on this ticket.'
  }
  for (const t of ticket.tasks_state ?? []) {
    const draft = want.tasks?.edit[t.id]
    if (!draft) continue
    const problem = taskProblem(draft)
    if (problem) return { problem }
    const change: NonNullable<Edit['tasks']>['edit'][string] = {}
    const text = draft.text === t.text ? t.text : draft.text.trim()
    const verify = draft.verify === (t.verify?.cmd ?? '') ? (t.verify?.cmd ?? null) : (draft.verify.trim() || null)
    const proves = sortProves(draft.proves)
    if (text !== t.text) change.text = text
    if (verify !== (t.verify?.cmd ?? null)) change.verify = verify
    if (JSON.stringify(proves) !== JSON.stringify(sortProves(t.proves))) change.proves = proves
    if (t.state === 'done' && ('verify' in change || 'proves' in change)) return { problem: `Task ${t.id} is done: its check and criteria cannot change because its receipt stands for them. Reopen it first.` }
    if (Object.keys(change).length) tasks.edit[t.id] = change
  }
  for (const draft of want.tasks?.add ?? []) {
    const problem = taskProblem(draft)
    if (problem) return { problem }
    tasks.add.push({ text: draft.text.trim(), ...(draft.verify.trim() ? { verify: draft.verify.trim() } : {}), proves: sortProves(draft.proves) })
  }
  let links: Edit['links']
  if (want.links) {
    const was = { repos: ticket.links?.repos ?? [], branches: ticket.links?.branches ?? {} }
    const repos = [...new Set(want.links.repos)].sort()
    const branches: Record<string, string> = {}
    for (const r of repos) {
      const b = (want.links.branches[r] ?? '').trim()
      const bad = branchProblem(b)
      if (bad) return { problem: `Branch for ${r}: ${bad}` }
      if (b) branches[r] = b
    }
    if (repos.length > MAX_LINK_REPOS) return { problem: `A ticket can be linked to at most ${MAX_LINK_REPOS} repositories.` }
    const same = repos.length === was.repos.length && repos.every((r) => was.repos.includes(r)) && repos.every((r) => (branches[r] ?? '') === (was.branches[r] ?? '')) && Object.keys(was.branches).length === Object.keys(branches).length
    if (!same) links = { repos, branches }
  }
  const people: NonNullable<Edit['people']> = {}
  if (mayEditPeople) {
    if (want.people.owner && want.people.owner !== ticket.people.owner) people.owner = { add: [want.people.owner], remove: [] }
    for (const role of ['assignees', 'reviewers'] as const) {
      const was = ticket.people[role]
      const now = [...new Set(want.people[role])]
      const added = now.filter((p) => !was.includes(p)).sort()
      const removed = was.filter((p) => !now.includes(p)).sort()
      if (added.length || removed.length) people[role] = { add: added, remove: removed }
    }
  }
  // R3 part B: pictures pasted into the changed sections are signed first (a file the ticket already has needs no signature)
  const texts = Object.values(sections)
  if (missingPasted(texts).some((n) => !(ticket.artifacts ?? []).some((a) => a.name === n))) return { problem: 'A pasted picture is no longer here (the page was reloaded). Paste it again, or take its line out of the text.' }
  const images = pastedIn(texts).filter((i) => !(ticket.artifacts ?? []).some((a) => a.name === i.name))
  if (images.length > MAX_IMAGES) return { problem: `At most ${MAX_IMAGES} pictures fit in one signing run.` }
  const taskSteps = Object.keys(tasks.edit).length + tasks.add.length
  const steps = (title ? 1 : 0) + images.length + taskSteps + Object.keys(sections).length + Object.keys(edit).length + add.length + (links ? 1 : 0) + Object.keys(people).length
  if (steps === 0) return { nothing: true }
  if (steps > MAX_SEQUENCE_STEPS) return { problem: `That is ${steps} separate changes and one signing run carries at most ${MAX_SEQUENCE_STEPS}. Sign some now and the rest after.` }
  return {
    steps,
    action: {
      action: 'edit_sequence',
      key: ticket.key,
      head_seq: ticket.head.seq,
      ...(title ? { title } : {}),
      ...(Object.keys(sections).length ? { sections } : {}),
      ...(Object.keys(edit).length || add.length ? { acceptance: { edit, add } } : {}),
      ...(taskSteps ? { tasks } : {}),
      ...(links ? { links } : {}),
      ...(Object.keys(people).length ? { people } : {}),
      ...(images.length ? { images } : {}),
    } as Edit,
  }
}

/** One shared rule and one dialog for the overview and row shortcuts. */
export function canEditTicket(ticket: Pick<TicketDocument, 'status' | 'head'>, role: Viewer['role'], handoff: boolean) {
  return handoff && can(role, 'ticket.act') && ticket.status !== 'done' && ticket.head.seq >= 1
}
const EditContext = createContext<((focus?: EditFocus) => void) | null>(null)
export const useTicketEdit = () => useContext(EditContext)

export function TicketEditProvider({ ticket, viewer, children }: { ticket: TicketDocument; viewer: Pick<Viewer, 'person' | 'role' | 'members'>; children: ReactNode }) {
  const [open, setOpen] = useState<{ focus?: EditFocus; full?: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const keySeq = useRef(ticket.head.seq)
  if (!busy) keySeq.current = ticket.head.seq
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  const allowed = canEditTicket(ticket, viewer.role, !!ws && handoffMode(ws.id))
  return <EditContext.Provider value={allowed ? (focus) => setOpen({ focus }) : null}>
    {children}
    {open && open.focus?.kind === 'title' && !open.full && <RenameDialog key={`rename-${keySeq.current}`} ticket={ticket} onBusy={setBusy} onFull={() => setOpen({ focus: { kind: 'title' }, full: true })} onClose={() => setOpen(null)} />}
    {open && !(open.focus?.kind === 'title' && !open.full) && <EditDialog key={keySeq.current} ticket={ticket} viewer={viewer} focus={open.focus} onBusy={setBusy} onClose={() => setOpen(null)} />}
  </EditContext.Provider>
}

/**
 * Renaming is one line, not the long form (r10-P09): the new title, Save, then the one signing review (the same
 * `edit_sequence` with only the title). The review opens by itself (prepare only, never armed): Sign is the person's own press.
 */
function RenameDialog({ ticket, onBusy, onFull, onClose }: { ticket: TicketDocument; onBusy: (busy: boolean) => void; onFull: () => void; onClose: () => void }) {
  const qc = useQueryClient()
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  const [title, setTitle] = useState(ticket.title ?? '')
  const [problem, setProblem] = useState<string | null>(null)
  const [signing, setSigning] = useState<Edit | null>(null)
  useEffect(() => { onBusy(signing !== null) }, [signing, onBusy])
  const save = () => {
    const built = buildEdit(ticket, { title, sections: {}, criteria: {}, added: [], people: { owner: ticket.people.owner, assignees: [...ticket.people.assignees], reviewers: [...ticket.people.reviewers] } }, false)
    if ('problem' in built) return setProblem(built.problem)
    if ('nothing' in built) return setProblem('The title is the same as before.')
    setProblem(null)
    setSigning(built.action)
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !signing && onClose()}>
      <DialogContent aria-label={`Rename ${ticket.key}`} className="sm:max-w-lg" onOpenAutoFocus={(e) => { e.preventDefault(); const f = document.getElementById('rename-title') as HTMLInputElement | null; if (f) { f.focus(); f.setSelectionRange(f.value.length, f.value.length) } }}>
        <DialogHeader>
          <DialogTitle>Rename {ticket.key}</DialogTitle>
          <DialogDescription>Write the new title, then confirm once.</DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save() }} className="space-y-2">
          <Input id="rename-title" aria-label="Title" autoFocus readOnly={!!signing} value={title} maxLength={MAX_TITLE + 50} onChange={(e) => setTitle(e.target.value)} />
          {problem && <p role="alert" className="text-[13px] text-danger">{problem}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" disabled={!!signing} onClick={onFull}>More fields…</Button>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!!signing}>Rename</Button>
          </DialogFooter>
        </form>
        {signing && ws && (
          <SignPopover
            title={`Rename ${ticket.key}`}
            ws={ws.id}
            action={signing}
            prepareOnOpen
            onClose={() => setSigning(null)}
            onSettled={(phase) => {
              if (phase !== 'done') return
              void qc.invalidateQueries()
              toast.success(`${ticket.key} renamed`)
              setSigning(null)
              onClose()
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

export function EditTicket(_props: { ticket: TicketDocument; viewer: Pick<Viewer, 'person' | 'role' | 'members'> }) {
  const edit = useTicketEdit()
  return edit ? <Button type="button" variant="outline" size="sm" onClick={() => edit()}><Pencil />Edit text, checks, repositories and people</Button> : null
}

export function EditDialog({ ticket, viewer, focus, onClose, onBusy }: { ticket: TicketDocument; viewer: Pick<Viewer, 'person' | 'role' | 'members'>; focus?: EditFocus; onClose: () => void; onBusy: (busy: boolean) => void }) {
  const qc = useQueryClient()
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  const mayEditPeople = viewer.role === 'owner' || viewer.role === 'maintainer' || ticket.people.owner === viewer.person
  const [title, setTitle] = useState(ticket.title ?? '')
  const [sections, setSections] = useState<Partial<Record<SectionName, string>>>(() => ({ ...ticket.body }))
  const [criteria, setCriteria] = useState<Record<string, string>>(() => Object.fromEntries(ticket.acceptance_state.map((a) => [a.id, a.text])))
  const [added, setAdded] = useState<string[]>([])
  const focusInput = useRef<HTMLInputElement | null>(null)
  const [tasks, setTasks] = useState<TaskDrafts>(() => ({ edit: Object.fromEntries(ticket.tasks_state.map(t => [t.id, { text: t.text, verify: t.verify?.cmd ?? '', proves: [...t.proves] }])), add: [] }))
  const [next, setNext] = useState('')
  const [people, setPeople] = useState<PeopleValue>(() => ({ owner: ticket.people.owner, assignees: [...ticket.people.assignees], reviewers: [...ticket.people.reviewers] }))
  const [links, setLinks] = useState<{ repos: string[]; branches: Record<string, string> }>(() => ({ repos: [...ticket.links.repos], branches: { ...ticket.links.branches } }))
  const [signing, setSigning] = useState<Edit | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  useEffect(() => { onBusy(signing !== null) }, [signing, onBusy])
  const acId = useId()
  const built = useMemo(() => buildEdit(ticket, { title, sections, criteria, added, people, links, tasks }, mayEditPeople), [ticket, title, sections, criteria, added, people, links, tasks, mayEditPeople])
  const shown = SECTION_ORDER.filter((n) => SECTIONS_BY_TYPE[ticket.type][n] !== 'absent' && n !== 'current_state')
  // K10: anything changed (even something not signable yet) is asked about before it is thrown away.
  const [asking, setAsking] = useState(false)
  const dirty = !('nothing' in built)
  const requestClose = () => { if (dirty) setAsking(true); else onClose() }
  const addNext = () => {
    const t = next.trim()
    if (!t) return
    setAdded((a) => [...a, t])
    setNext('')
  }
  const review = () => {
    if ('problem' in built) return setProblem(built.problem)
    if ('nothing' in built) return setProblem('Nothing has changed yet.')
    setProblem(null)
    setSigning(built.action)
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !signing && !asking && requestClose()}>
      <DialogContent onOpenAutoFocus={(event) => { if (focusInput.current) { event.preventDefault(); focusInput.current.focus(); if (focus?.kind === 'title') focusInput.current.select(); focusInput.current.scrollIntoView?.({ block: 'center' }) } }} className="max-h-[90vh] overflow-y-auto sm:max-w-3xl" aria-label={`Edit ${ticket.key}`}>
        <DialogHeader>
          <DialogTitle>Edit {ticket.key}</DialogTitle>
          <DialogDescription>
            Change the title, text, acceptance criteria, tasks, repositories and people, then sign once. All changes are shown together in one review; if anyone else changes the ticket meanwhile, signing stops.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-5">
          <div className="space-y-1.5">
            <label htmlFor={`${acId}-title`} className="text-[13px] font-medium">Title</label>
            <Input id={`${acId}-title`} ref={focus?.kind === 'title' ? focusInput : undefined} value={title} maxLength={MAX_TITLE + 50} onChange={(e) => setTitle(e.target.value)} />
          </div>
          {shown.map((n) => (
            <Section key={n} type={ticket.type} name={n} value={sections[n] ?? ''} onChange={(v) => setSections((s) => ({ ...s, [n]: v }))} />
          ))}
          <section className="space-y-1.5" aria-labelledby={acId}>
            <h2 id={acId} className="text-[13px] font-medium">
              Acceptance criteria
            </h2>
            {ticket.acceptance_state.length > 0 && (
              <ol className="space-y-1">
                {ticket.acceptance_state.map((a) => (
                  <li key={a.id} className="flex items-center gap-2">
                    <span className="w-9 font-mono text-[11px] text-text-faint">{a.id}</span>
                    <Input ref={focus?.kind === 'ac' && focus.id === a.id ? focusInput : undefined} aria-label={`Text of ${a.id}`} value={criteria[a.id] ?? a.text} onChange={(e) => setCriteria((c) => ({ ...c, [a.id]: e.target.value }))} className="h-8 text-[13px]" />
                  </li>
                ))}
              </ol>
            )}
            {added.length > 0 && (
              <ol className="space-y-1">
                {added.map((text, i) => (
                  <li key={i} className="flex items-center gap-2 rounded-md border border-border bg-surface px-2.5 py-1.5 text-[13px]">
                    <span className="font-mono text-[11px] text-text-faint">new</span>
                    <span className="flex-1">{text}</span>
                    <Button type="button" variant="ghost" size="icon" className="size-6" aria-label={`Remove new criterion ${i + 1}`} onClick={() => setAdded((a) => a.filter((_, j) => j !== i))}>
                      <X className="size-3.5" />
                    </Button>
                  </li>
                ))}
              </ol>
            )}
            <div className="flex gap-2">
              <Input
                value={next}
                onChange={(e) => setNext(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) {
                    e.preventDefault()
                    addNext()
                  }
                }}
                placeholder="Something that must be provable"
                aria-label="New acceptance criterion"
                className="h-8 text-[13px]"
              />
              <Button type="button" variant="outline" size="sm" onClick={addNext}>
                <Plus />
                Add
              </Button>
            </div>
          </section>
          <section className="space-y-3" aria-label="Tasks">
            <h2 className="text-[13px] font-medium">Tasks</h2>
            <p className="text-[12px] text-text-muted">Choose existing criteria for each task. New criteria can be linked after this edit is signed. Leave the check empty when a person checks the task.</p>
            {[...ticket.tasks_state.map(t => ({ id: t.id, draft: tasks.edit[t.id], done: t.state === 'done', index: -1 })), ...tasks.add.map((draft, index) => ({ id: `new task ${index + 1}`, draft, done: false, index }))].map(({ id, draft, done, index }) => {
              const update = (change: Partial<TaskDraft>) => setTasks(old => index < 0 ? { ...old, edit: { ...old.edit, [id]: { ...old.edit[id], ...change } } } : { ...old, add: old.add.map((t, i) => i === index ? { ...t, ...change } : t) })
              return <div key={id} className="space-y-1.5">
                <p className="text-[12px] font-medium">{index < 0 ? `Task ${ticket.tasks_state.findIndex(t => t.id === id) + 1}` : `New task ${index + 1}`}</p>
                <Input ref={focus?.kind === 'task' && focus.id === id ? focusInput : undefined} aria-label={`Text of ${id}`} placeholder="What needs doing" value={draft.text} onChange={e => update({ text: e.target.value })} />
                <label className="block text-[12px] text-text-muted">How it is checked
                  <Input aria-label={`Check command of ${id}`} disabled={done} value={draft.verify} onChange={e => update({ verify: e.target.value })} placeholder="Checked by a person" />
                </label>
                <div className="flex flex-wrap gap-1">{ticket.acceptance_state.map(a => <Button key={a.id} type="button" size="sm" className="h-auto min-h-8 min-w-0 max-w-full whitespace-normal text-left" variant={draft.proves.includes(a.id) ? 'secondary' : 'outline'} disabled={done} aria-label={`${id} proves ${a.id}`} aria-pressed={draft.proves.includes(a.id)} onClick={() => update({ proves: draft.proves.includes(a.id) ? draft.proves.filter(x => x !== a.id) : [...draft.proves, a.id] })}>{criteria[a.id] || a.text}</Button>)}</div>
                {done && <p className="text-[12px] text-text-muted">Its receipt stands for this check and these criteria. Reopen the task to change them.</p>}
                {index >= 0 && <Button type="button" variant="ghost" size="sm" onClick={() => setTasks(old => ({ ...old, add: old.add.filter((_, i) => i !== index) }))}>Remove new task {index + 1}</Button>}
              </div>
            })}
            <Button type="button" variant="outline" size="sm" onClick={() => setTasks(old => ({ ...old, add: [...old.add, { text: '', verify: '', proves: [] }] }))}><Plus />Add task</Button>
          </section>
          <RepoLinks ticketKey={ticket.key} wsId={ws?.id} value={links} onChange={setLinks} />
          {mayEditPeople ? (
            <PeoplePicker members={viewer.members} creator={viewer.person} people={people} onPeople={setPeople} visibility="workspace" onVisibility={() => undefined} showVisibility={false} />
          ) : (
            <p className="text-[12px] text-text-muted">Only the ticket owner, owners and maintainers change who owns, works on or reviews this ticket.</p>
          )}
        </div>
        {problem && (
          <p role="alert" className="text-[13px] text-danger">
            {problem}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={requestClose}>
            Cancel
          </Button>
          <Button type="button" onClick={review} disabled={!!signing}>
            Review and sign{'action' in built ? ` (${built.steps} ${built.steps === 1 ? 'change' : 'changes'})` : ''}
          </Button>
        </DialogFooter>
        {asking && (
          <Dialog open onOpenChange={(o) => !o && setAsking(false)}>
            <DialogContent aria-label="Discard unsaved changes?">
              <DialogHeader>
                <DialogTitle>Discard unsaved changes?</DialogTitle>
                <DialogDescription>You changed this ticket's title, text, checks, repositories or people. Closing now throws those changes away.</DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setAsking(false)}>Keep editing</Button>
                <Button type="button" variant="destructive" onClick={onClose}>Discard changes</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
        {signing && ws && (
          <SignPopover
            title={`Edit ${ticket.key}`}
            ws={ws.id}
            action={signing}
            prepareOnOpen
            onClose={() => setSigning(null)}
            onSettled={(phase) => {
              if (phase !== 'done') return
              void qc.invalidateQueries()
              toast.success(`${ticket.key} edited`)
              setSigning(null)
              onClose()
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

/** The repository names the workspace lists (Settings → Repos): the host's list on a real host, the workspace's own otherwise. */
function useRepoNames(wsId: string | undefined, fallback: Record<string, unknown> | undefined): { names: string[]; ready: boolean } {
  const settings = useQuery({ ...queries.hostSettings(wsId ?? ''), enabled: !!wsId })
  const fromHost = settings.data?.repos?.map((r) => r.name)
  const names = [...new Set([...(fromHost ?? []), ...Object.keys(fallback ?? {})])].sort()
  return { names, ready: !settings.isPending || names.length > 0 }
}

/** F6: which repositories this ticket belongs to and the branch of each. Picked from the workspace's list; the host checks the names again and the change is part of the ONE review. */
export function RepoLinks({
  ticketKey,
  wsId,
  value,
  onChange,
}: {
  ticketKey: string
  wsId: string | undefined
  value: { repos: string[]; branches: Record<string, string> }
  onChange: (v: { repos: string[]; branches: Record<string, string> }) => void
}) {
  const id = useId()
  const workspaces = useQuery(queries.workspaces())
  const fallback = workspaces.data?.find((w) => w.id === wsId)?.repos
  const { names, ready } = useRepoNames(wsId, fallback)
  const [pick, setPick] = useState('')
  const free = names.filter((n) => !value.repos.includes(n))
  const chosen = free.includes(pick) ? pick : (free[0] ?? '')
  const link = () => {
    if (!chosen || value.repos.length >= MAX_LINK_REPOS) return
    onChange({ repos: [...value.repos, chosen], branches: { ...value.branches, [chosen]: suggestedBranch(ticketKey) } })
    setPick('')
  }
  const unlink = (repo: string) => {
    const { [repo]: _gone, ...rest } = value.branches
    onChange({ repos: value.repos.filter((r) => r !== repo), branches: rest })
  }
  return (
    <section className="space-y-1.5" aria-labelledby={id} data-testid="edit-repositories">
      <h2 id={id} className="text-[13px] font-medium">
        Repositories
      </h2>
      <p className="text-[12px] text-text-muted">The code of this ticket lives in these repositories. Name the branch it is worked on; agents and worktrees use it. You can leave a branch empty for now.</p>
      {value.repos.length > 0 && (
        <ul className="space-y-1">
          {value.repos.map((repo) => {
            const bad = branchProblem(value.branches[repo] ?? '')
            const listed = names.includes(repo)
            return (
              <li key={repo} className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="w-28 shrink-0 truncate text-[13px] font-medium" title={repo}>
                    {repo}
                  </span>
                  <Input
                    aria-label={`Branch in ${repo}`}
                    aria-invalid={bad ? true : undefined}
                    placeholder={suggestedBranch(ticketKey)}
                    value={value.branches[repo] ?? ''}
                    onChange={(e) => onChange({ ...value, branches: { ...value.branches, [repo]: e.target.value } })}
                    className="h-8 text-[13px]"
                  />
                  <Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Unlink ${repo}`} onClick={() => unlink(repo)}>
                    <X className="size-3.5" />
                  </Button>
                </div>
                {bad && (
                  <p role="alert" className="ml-[7.5rem] text-[12px] text-danger">
                    {bad}
                  </p>
                )}
                {ready && !listed && <p className="text-[12px] text-warning">{repo} is not in the workspace's repository list any more.</p>}
              </li>
            )
          })}
        </ul>
      )}
      {names.length === 0 && ready ? (
        <p className="text-[12px] text-text-muted">The workspace has no repositories yet. An owner adds them under Settings, General, Repositories.</p>
      ) : free.length > 0 ? (
        <div className="flex items-center gap-2">
          <select aria-label="Repository to link" className="h-8 min-w-0 flex-1 rounded border border-border bg-bg px-2 text-[13px]" value={chosen} onChange={(e) => setPick(e.target.value)}>
            {free.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <Button type="button" variant="outline" size="sm" onClick={link} disabled={value.repos.length >= MAX_LINK_REPOS}>
            <Plus />
            Link repository
          </Button>
        </div>
      ) : (
        names.length > 0 && <p className="text-[12px] text-text-muted">Every repository of the workspace is linked.</p>
      )}
    </section>
  )
}
