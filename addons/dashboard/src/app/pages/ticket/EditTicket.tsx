// B12D (finding N3): edit an existing ticket's text, acceptance criteria, repositories and people, then ONE review.
//
// The page sends what changed plus the log head it showed (`head_seq`); the server reads the ticket, refuses the edit
// when the ticket has changed since, and the signer signs one ordinary event per change, each checked against the
// reviewed intent. Nothing here signs or names an op: it builds the typed `edit_sequence` action.
import { Pencil, Plus, X } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { handoffMode } from '@/api/features'
import { can } from '@/api/permissions'
import { MAX_CRITERIA, MAX_SECTION_BYTES, MAX_SEQUENCE_STEPS, type HandoffAction } from '@/api/handoff'
import { SECTIONS_BY_TYPE, SECTION_ORDER, sectionLabel, type SectionName } from '@/api/sections'
import { branchProblem, MAX_LINK_REPOS, suggestedBranch } from '@/api/links'
import type { TicketDocument, TicketLinks } from '@/api/types'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { workspaceOfTicket } from '@/api/workspaces'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { Section } from '../new-ticket/SectionsEditor'
import { PeoplePicker, type PeopleValue } from '../new-ticket/PeoplePicker'
import type { Viewer } from './shared'

type Edit = NonNullable<Extract<HandoffAction, { action: 'edit_sequence' }>>

/** What the person changed, as the typed action; or what is wrong with it. Pure: tested without the dialog. */
export function buildEdit(
  ticket: Pick<TicketDocument, 'key' | 'type' | 'body' | 'acceptance_state' | 'people' | 'head'> & { links?: Pick<TicketLinks, 'repos' | 'branches'> },
  want: { sections: Partial<Record<SectionName, string>>; criteria: Record<string, string>; added: string[]; people: PeopleValue; links?: { repos: string[]; branches: Record<string, string> } },
  mayEditPeople: boolean,
): { action: Edit; steps: number } | { problem: string } | { nothing: true } {
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
  const steps = Object.keys(sections).length + Object.keys(edit).length + add.length + (links ? 1 : 0) + Object.keys(people).length
  if (steps === 0) return { nothing: true }
  if (steps > MAX_SEQUENCE_STEPS) return { problem: `That is ${steps} separate changes and one signing run carries at most ${MAX_SEQUENCE_STEPS}. Sign some now and the rest after.` }
  return {
    steps,
    action: {
      action: 'edit_sequence',
      key: ticket.key,
      head_seq: ticket.head.seq,
      ...(Object.keys(sections).length ? { sections } : {}),
      ...(Object.keys(edit).length || add.length ? { acceptance: { edit, add } } : {}),
      ...(links ? { links } : {}),
      ...(Object.keys(people).length ? { people } : {}),
    } as Edit,
  }
}

/** The button and the dialog. Offered where the viewer may change the ticket and the host signs it. */
export function EditTicket({ ticket, viewer }: { ticket: TicketDocument; viewer: Pick<Viewer, 'person' | 'role' | 'members'> }) {
  const [open, setOpen] = useState(false)
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  // Only where the host signs (hand-off), for a member, on a ticket that is not finished, read in full (a head to chain on).
  if (!ws || !handoffMode(ws.id) || !can(viewer.role, 'ticket.act') || ticket.status === 'done' || ticket.head.seq < 1) return null
  return (
    <>
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
        <Pencil />
        Edit text, checks, repositories and people
      </Button>
      {open && <EditDialog key={ticket.head.seq} ticket={ticket} viewer={viewer} onClose={() => setOpen(false)} />}
    </>
  )
}

function EditDialog({ ticket, viewer, onClose }: { ticket: TicketDocument; viewer: Pick<Viewer, 'person' | 'role' | 'members'>; onClose: () => void }) {
  const qc = useQueryClient()
  const workspaces = useQuery(queries.workspaces())
  const ws = workspaceOfTicket(ticket.key, workspaces.data ?? [])
  const mayEditPeople = viewer.role === 'owner' || viewer.role === 'maintainer' || ticket.people.owner === viewer.person
  const [sections, setSections] = useState<Partial<Record<SectionName, string>>>(() => ({ ...ticket.body }))
  const [criteria, setCriteria] = useState<Record<string, string>>(() => Object.fromEntries(ticket.acceptance_state.map((a) => [a.id, a.text])))
  const [added, setAdded] = useState<string[]>([])
  const [next, setNext] = useState('')
  const [people, setPeople] = useState<PeopleValue>(() => ({ owner: ticket.people.owner, assignees: [...ticket.people.assignees], reviewers: [...ticket.people.reviewers] }))
  const [links, setLinks] = useState<{ repos: string[]; branches: Record<string, string> }>(() => ({ repos: [...ticket.links.repos], branches: { ...ticket.links.branches } }))
  const [signing, setSigning] = useState<Edit | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const acId = useId()
  const built = useMemo(() => buildEdit(ticket, { sections, criteria, added, people, links }, mayEditPeople), [ticket, sections, criteria, added, people, links, mayEditPeople])
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl" aria-label={`Edit ${ticket.key}`}>
        <DialogHeader>
          <DialogTitle>Edit {ticket.key}</DialogTitle>
          <DialogDescription>
            Change the text, the acceptance checks, the repositories and the people, then sign once. All changes are shown together in one review; if anyone else changes the ticket meanwhile, signing stops.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-5">
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
                    <Input aria-label={`Text of ${a.id}`} value={criteria[a.id] ?? a.text} onChange={(e) => setCriteria((c) => ({ ...c, [a.id]: e.target.value }))} className="h-8 text-[13px]" />
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
                <DialogDescription>You changed this ticket's text, checks, repositories or people. Closing now throws those changes away.</DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setAsking(false)}>Keep editing</Button>
                <Button type="button" variant="destructive" onClick={onClose}>Discard changes</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
        {signing && ws && (
          <HandoffDialog
            title={`Edit ${ticket.key}`}
            ws={ws.id}
            action={signing}
            autoStart
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
        <p className="text-[12px] text-text-muted">The workspace has no repositories yet. An owner adds them under Settings, Repositories.</p>
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
