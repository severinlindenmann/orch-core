// Settings > Profile (hub-full HUB-WS-09, #1390): what this workspace does, written once by its owner. The hub shows it
// to people and agents read it to find the right workspace. Editing works without a hub; the HOST sends it to a hub
// once one is connected. An owner click, no typed word, nothing signed here (see server profile_routes.py).
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { ACCEPT_CHOICES, EMPTY_PROFILE, PROFILE_LIMITS, cleanProfile, profileQueries, type ProfileFields } from '@/api/profile'
import { ApiError, type Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Details, Field, Panel, useHub } from './hub/shared'
import { useViewerRole } from './host/useViewerRole'

const lines = (v: string[]) => v.join('\n')
const splitLines = (v: string) => v.split('\n')
const splitCommas = (v: string) => v.split(',')
const area = 'min-h-16 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm'

export function Profile({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const role = useViewerRole(workspace)
  const hub = useHub(ws)
  const qc = useQueryClient()
  const owner = role === 'owner'
  const got = useQuery({ ...profileQueries.get(ws), enabled: owner })
  const [draft, setDraft] = useState<ProfileFields>(EMPTY_PROFILE)
  const [tags, setTags] = useState('')
  const [dirty, setDirty] = useState(false)
  const [state, setState] = useState<'idle' | 'saved' | 'saved-hub'>('idle')
  useEffect(() => {
    if (got.data && !dirty) { setDraft(got.data); setTags(got.data.tags.join(', ')) }
  }, [got.data, dirty])
  const save = useMutation({
    mutationFn: () => api.putHubProfile(ws, cleanProfile({ ...draft, tags: splitCommas(tags) })),
    onSuccess: (r) => { setDirty(false); setState(r.pushed ? 'saved-hub' : 'saved'); void qc.invalidateQueries({ queryKey: ['hub', ws, 'profile'] }) },
  })
  const edit = (patch: Partial<ProfileFields>) => { setDraft((d) => ({ ...d, ...patch })); setDirty(true); setState('idle'); save.reset() }

  if (!owner) return <Panel title="Profile"><p data-testid="profile-owner-only">Only the workspace owner changes the profile.</p></Panel>
  if (got.isPending) return <Panel title="Profile"><p role="status">Loading the profile…</p></Panel>
  if (got.isError) return <Panel title="Profile"><p role="alert">Could not load the profile.</p><Button variant="outline" onClick={() => void got.refetch()}>Try again</Button></Panel>
  const connected = hub.data?.joined === true && hub.data.mode !== 'standalone'
  const choices = [...ACCEPT_CHOICES, ...draft.accepts.filter((a) => !(ACCEPT_CHOICES as readonly string[]).includes(a))]
  return (
    <div className="space-y-4" data-testid="profile-tab">
      <Panel title="Profile">
        <p>What this workspace does, in a few words. {connected ? 'People see it in the hub, and agents use it to find the right workspace for a request.' : 'It is shown once a hub is connected. You can write it now.'}</p>
        <label className="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" data-testid="profile-listed" checked={draft.listed} onChange={(e) => edit({ listed: e.target.checked })} />
          Show this workspace in the hub
        </label>
        <div className="min-w-0 space-y-1">
          <label htmlFor="profile-description" className="block text-sm font-medium">What it does</label>
          <textarea id="profile-description" data-testid="profile-description" className={area} maxLength={PROFILE_LIMITS.description} value={draft.description} onChange={(e) => edit({ description: e.target.value })} />
        </div>
        <Field label="Tags" testId="profile-tags" value={tags} hint="Separate with commas." onChange={(v) => { setTags(v); setDirty(true); setState('idle'); save.reset() }} />
        <div className="min-w-0 space-y-1">
          <label htmlFor="profile-handles" className="block text-sm font-medium">What it handles</label>
          <textarea id="profile-handles" data-testid="profile-handles" className={area} value={lines(draft.handles)} onChange={(e) => edit({ handles: splitLines(e.target.value) })} />
          <p className="text-xs text-text-muted">One per line.</p>
        </div>
        <fieldset className="space-y-1"><legend className="text-sm font-medium">Requests it accepts</legend>
          <div className="flex flex-wrap gap-3">{choices.map((c) => (
            <label key={c} className="flex items-center gap-1.5 text-sm"><input type="checkbox" data-testid={`profile-accepts-${c}`} checked={draft.accepts.includes(c)} onChange={(e) => edit({ accepts: e.target.checked ? [...draft.accepts, c] : draft.accepts.filter((x) => x !== c) })} />{c}</label>
          ))}</div>
        </fieldset>
        <Details testId="profile-more">
          <div className="min-w-0 space-y-1">
            <label htmlFor="profile-not-handled" className="block text-sm font-medium text-text">What it does not handle</label>
            <textarea id="profile-not-handled" data-testid="profile-not-handled" className={area} value={lines(draft.not_handled)} onChange={(e) => edit({ not_handled: splitLines(e.target.value) })} />
            <p className="text-xs">One per line.</p>
          </div>
          <Field label="Contact" testId="profile-contact" value={draft.contact} onChange={(v) => edit({ contact: v })} />
          <Field label="Languages" testId="profile-languages" value={draft.languages} onChange={(v) => edit({ languages: v })} />
          <Field label="Hours" testId="profile-hours" value={draft.hours} onChange={(v) => edit({ hours: v })} />
        </Details>
        <div className="flex flex-wrap items-center gap-3">
          <Button data-testid="profile-save" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save</Button>
          {state === 'saved' && <span role="status" data-testid="profile-saved">Saved. {connected ? 'The hub is told when it can be reached.' : 'The hub shows it once a hub is connected.'}</span>}
          {state === 'saved-hub' && <span role="status" data-testid="profile-saved">Saved and sent to the hub.</span>}
          {save.isError && <span role="alert" data-testid="profile-error">{save.error instanceof ApiError ? save.error.message : 'Could not save the profile.'}</span>}
        </div>
        <Details testId="profile-privacy"><p>Only these fields leave this computer, and only to the hub you connected. Nothing else from the workspace is part of the profile. Everyone who can see this workspace in the hub reads it.</p></Details>
      </Panel>
    </div>
  )
}
