import { Pencil, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { canChangeSettings, repoCheckWords, repoProblems, type HostRepo, type HostSettings, type RepoDraft } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Workspace } from '@/api/types'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Mono, Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useSettingsHandoff } from './useSettingsHandoff'
import { useViewerRole } from './useViewerRole'

const BLANK: RepoDraft = { name: '', path: '', remote: '', branch: '' }

function Field({ id, label, help, value, error, disabled, placeholder, onChange }: { id: string; label: string; help: string; value: string; error?: string; disabled?: boolean; placeholder?: string; onChange: (v: string) => void }) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} value={value} disabled={disabled} placeholder={placeholder} autoComplete="off" spellCheck={false} aria-invalid={!!error || undefined} aria-describedby={`${id}-help`} onChange={(e) => onChange(e.target.value)} />
      <p id={`${id}-help`} className="text-[12px] text-text-faint">
        {help}
      </p>
      {error && (
        <p role="alert" className="text-[12px] text-danger">
          {error}
        </p>
      )}
    </div>
  )
}

/** Add or change one repository. Nothing is sent from here: "Review and sign" opens the signing hand-off with the typed action. */
function RepoForm({ ws, initial, editing, taken, onSubmit, onClose }: { ws: string; initial: RepoDraft; editing: boolean; taken: string[]; onSubmit: (d: RepoDraft) => void; onClose: () => void }) {
  const [d, setD] = useState(initial)
  const [tried, setTried] = useState(false)
  const [checking, setChecking] = useState(false)
  // The host's own look at the folder (home folder, not a git folder, orch's data, missing), shown next to the field.
  const [hostSaid, setHostSaid] = useState<{ path: string; text: string } | null>(null)
  const set = (k: keyof RepoDraft) => (v: string) => setD({ ...d, [k]: v })
  const problems = repoProblems(d)
  if (!editing && !problems.name && taken.includes(d.name)) problems.name = 'A repository with this name is already in the list.'
  const shown = (k: keyof RepoDraft) => (tried ? problems[k] : undefined) ?? (k === 'path' && hostSaid?.path === d.path ? hostSaid.text : undefined)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
        <form
          noValidate
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault()
            setTried(true)
            if (Object.keys(problems).length > 0 || checking) return
            setChecking(true)
            try {
              await api.checkRepo(ws, { action: 'repo_set', name: d.name, path: d.path, ...(d.remote ? { remote: d.remote } : {}), ...(d.branch ? { default_branch: d.branch } : {}) })
            } catch (err) {
              // Only the host's own plain refusal of the folder stops the form here; anything else (a host that cannot
              // be asked, a lost answer) goes on to the signing step, which checks again and says so.
              if (err instanceof ApiError && err.code === 'invalid.input') {
                setHostSaid({ path: d.path, text: err.message })
                setTried(false)
                setChecking(false)
                return
              }
            }
            setChecking(false)
            onSubmit(d)
          }}
        >
          <DialogHeader>
            <DialogTitle>{editing ? `Change ${d.name}` : 'Add a repository'}</DialogTitle>
            <DialogDescription>You sign this change in the next step, and it shows the old and the new values side by side. orch checks that the folder exists and is a git repository before anything is signed.</DialogDescription>
          </DialogHeader>
          <Field id="repo-name" label="Name" help="What tickets call it: lowercase letters, digits, dots and dashes." value={d.name} disabled={editing} error={shown('name')} onChange={set('name')} />
          <Field id="repo-path" label="Folder on this computer" help="The full path of the repository folder, for example /Users/you/code/billing." value={d.path} error={shown('path')} placeholder="/Users/you/code/…" onChange={set('path')} />
          <Field id="repo-remote" label="Remote (optional)" help="Where it lives online: an https:// address without a login. orch never stores logins." value={d.remote} error={shown('remote')} placeholder="https://github.com/you/repo" onChange={set('remote')} />
          <Field id="repo-branch" label="Main branch (optional)" help="The branch work is merged into, for example main." value={d.branch} error={shown('branch')} onChange={set('branch')} />
          <DialogFooter className="gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={checking}>{checking ? 'Checking the folder…' : 'Review and sign'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

const TONE: Record<string, string> = { present: 'text-success', missing: 'text-warning', not_git: 'text-warning', unreadable: 'text-warning' }

/** Settings → General → Repositories: the workspace's repos with what the host sees on disk, and, for owners, add / change / remove as a signed change. */
export function ReposSection({ workspace, data, onSettled }: { workspace: Workspace; data: HostSettings | undefined; onSettled: () => void }) {
  const role = useViewerRole(workspace)
  const { ask, dialog } = useSettingsHandoff(workspace.id, onSettled)
  const [form, setForm] = useState<{ draft: RepoDraft; editing: boolean } | null>(null)
  const [removing, setRemoving] = useState<HostRepo | null>(null)
  const repos: HostRepo[] = data?.repos ?? []
  const served = hasFeature('do:settings.set', workspace.id)
  const mayEdit = canChangeSettings(role) && served
  const why = !canChangeSettings(role) ? 'Only owners can add, change or remove repositories.' : !served ? 'This host cannot change the repository list yet.' : null

  const open = (r?: HostRepo) =>
    setForm(r ? { editing: true, draft: { name: r.name, path: r.path ?? '', remote: r.remote ?? '', branch: r.default_branch ?? '' } } : { editing: false, draft: BLANK })
  const submit = (d: RepoDraft) => {
    setForm(null)
    ask({
      title: form?.editing ? `Change repository ${d.name}` : `Add repository ${d.name}`,
      action: { action: 'repo_set', name: d.name, path: d.path, ...(d.remote ? { remote: d.remote } : {}), ...(d.branch ? { default_branch: d.branch } : {}) },
      autoStart: true,
    })
  }

  const removeRepo = (name: string) => ask({ title: `Remove repository ${name}`, action: { action: 'repo_remove', name }, autoStart: true })

  return (
    <Section title="Repositories">
      {!data ? (
        <p className="text-[13px] text-text-muted">Reading the repositories…</p>
      ) : !data.available || !data.repos ? (
        <p className="text-[13px] text-text-muted">This host does not say which repositories there are.</p>
      ) : (
        <>
          {repos.length === 0 ? (
            <p className="text-[13px] text-text-muted">No project folders yet.</p>
          ) : (
            <ul aria-label="Repositories" className="divide-y divide-border text-[13px]">
              {repos.map((r) => (
                <li key={r.name} className="flex flex-wrap items-start gap-x-4 gap-y-0.5 py-2">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-x-3">
                      <Mono><Raw>{r.name}</Raw></Mono>
                      {r.check && (
                        <span data-testid={`repo-check-${r.name}`} className={TONE[r.check.state] ?? 'text-text-muted'}>
                          {repoCheckWords(r.check)}
                        </span>
                      )}
                    </div>
                    {r.path && <div className="break-all text-text-muted"><Raw>{r.path}</Raw></div>}
                    <div className="flex flex-wrap gap-x-3 text-text-faint">
                      <span>{r.default_branch === null ? 'no main branch set' : <>main branch <Raw>{r.default_branch}</Raw></>}</span>
                      {r.remote && <span className="min-w-0 break-all"><Raw>{r.remote}</Raw></span>}
                    </div>
                  </div>
                  {mayEdit && (
                    <div className="flex gap-1">
                      <Button variant="ghost" size="sm" aria-label={`Change ${r.name}`} disabled={!r.path} title={r.path ? undefined : 'The host did not send the folder, so it cannot be changed here.'} onClick={() => open(r)}>
                        <Pencil />
                      </Button>
                      <Button variant="ghost" size="sm" aria-label={`Remove ${r.name}`} onClick={() => (r.tickets?.length ? setRemoving(r) : removeRepo(r.name))}>
                        <Trash2 />
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          {data.repos_more && <p className="mt-2 text-[12px] text-text-muted">More repositories exist than are listed here.</p>}
          <div className="mt-3 flex flex-wrap items-center gap-3">
            {mayEdit && (
              <Button size="sm" variant="outline" onClick={() => open()}>
                <Plus />
                Add folder
              </Button>
            )}
            {why && <OwnerNote id="repos-why">{why}</OwnerNote>}
          </div>
          <p className="mt-2 text-[12px] text-text-muted">
            A change to this list is signed like any other setting. Removing a repository only takes it off the list: its folder and files stay on disk. orch does not download (clone) repositories yet: add the folder after cloning it yourself.
          </p>
        </>
      )}
      {form && <RepoForm ws={workspace.id} initial={form.draft} editing={form.editing} taken={repos.map((r) => r.name)} onSubmit={submit} onClose={() => setForm(null)} />}
      {removing && (
        <Dialog open onOpenChange={(o) => !o && setRemoving(null)}>
          <DialogContent className="max-w-md gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Remove {removing.name}?</DialogTitle>
              <DialogDescription>
                {removing.tickets!.length === 1 ? 'This ticket links' : 'These tickets link'} this repository: <Raw>{removing.tickets!.join(', ')}</Raw>
                {removing.tickets!.length >= 12 ? ' and maybe more' : ''}. {removing.tickets!.length === 1 ? 'It keeps' : 'They keep'} the link, but no one can add a worktree for {removing.tickets!.length === 1 ? 'it' : 'them'} until you add the repository again or link another one. The folder and its files stay on disk.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter className="gap-2">
              <Button type="button" variant="ghost" onClick={() => setRemoving(null)}>
                Keep it
              </Button>
              <Button type="button" variant="destructive" onClick={() => { const n = removing.name; setRemoving(null); removeRepo(n) }}>
                Continue to review and sign
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {dialog}
    </Section>
  )
}
