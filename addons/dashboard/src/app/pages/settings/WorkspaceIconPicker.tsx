import { useState } from 'react'
import { Check } from 'lucide-react'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { WORKSPACE_COLOURS, WORKSPACE_ICONS, WorkspaceIcon } from '@/app/workspaceIcon'
import { defaultWorkspaceIcon, workspaceIconOf, type WorkspaceIconChoice } from '@/api/wsIcons'
import { OwnerNote } from './OwnerNote'

export const iconWords = (c: WorkspaceIconChoice) => `${WORKSPACE_ICONS.find((i) => i.id === c.icon)?.label ?? c.icon}, ${WORKSPACE_COLOURS.find((x) => x.id === c.colour)?.label ?? c.colour}`

/**
 * Picks the icon and colour shown for this workspace in the left menu. It is a signed workspace setting (the owner
 * signs it, everyone sees it, the hub shows it too), so the choice is a draft until `onSave` signs it. Hidden when the
 * host does not report the setting (an older host).
 */
export function WorkspaceIconPicker({ workspace, canEdit, onSave }: { workspace: Workspace; canEdit: boolean; onSave: (choice: WorkspaceIconChoice | null) => void }) {
  const saved = workspaceIconOf(workspace)
  const auto = defaultWorkspaceIcon(workspace.id)
  // The draft is what the preview shows; `automatic` is the draft "go back to the automatic icon" (saved as null).
  const [draft, setDraft] = useState<WorkspaceIconChoice | 'automatic' | null>(null)
  const shown = draft === 'automatic' ? auto : (draft ?? saved)
  const wantsAuto = draft === 'automatic' || (draft === null && !workspace.icon)
  const changed = draft === 'automatic' ? !!workspace.icon : draft !== null && (draft.icon !== saved.icon || draft.colour !== saved.colour)
  const pick = (c: WorkspaceIconChoice) => setDraft(c)
  if (workspace.icon_known === false) return null
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-3">
        <WorkspaceIcon {...shown} />
        <p className="min-w-0 flex-1 text-[13px] text-text-muted">Choose how this workspace appears in the left menu. Everyone in the workspace sees it. The prefix and name always stay next to it.</p>
      </div>
      <fieldset className="space-y-1.5">
        <legend className="text-[12px] text-text-muted">Symbol</legend>
        <div className="flex flex-wrap gap-1.5">
          {WORKSPACE_ICONS.map((i) => (
            <button
              key={i.id}
              type="button"
              aria-label={i.label}
              aria-pressed={shown.icon === i.id}
              disabled={!canEdit}
              onClick={() => pick({ ...shown, icon: i.id })}
              className={cn('rounded-md p-0.5 outline-none focus-visible:ring-2 focus-visible:ring-brand disabled:opacity-60', shown.icon === i.id ? 'bg-surface-3 ring-2 ring-text' : 'hover:bg-surface-2')}
            >
              <WorkspaceIcon icon={i.id} colour={shown.colour} />
            </button>
          ))}
        </div>
      </fieldset>
      <fieldset className="space-y-1.5">
        <legend className="text-[12px] text-text-muted">Colour</legend>
        <div className="flex flex-wrap gap-1.5">
          {WORKSPACE_COLOURS.map((c) => (
            <button
              key={c.id}
              type="button"
              aria-label={c.label}
              aria-pressed={shown.colour === c.id}
              disabled={!canEdit}
              onClick={() => pick({ ...shown, colour: c.id })}
              style={{ background: c.value }}
              className={cn('flex size-7 items-center justify-center rounded-full text-white outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2 disabled:opacity-60', shown.colour === c.id && 'ring-2 ring-text ring-offset-2')}
            >
              {shown.colour === c.id && <Check aria-hidden className="size-3.5" />}
            </button>
          ))}
        </div>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!canEdit || !changed} onClick={() => onSave(draft === 'automatic' ? null : (draft as WorkspaceIconChoice))} aria-describedby={canEdit ? undefined : 'ws-icon-why'}>
          Save
        </Button>
        <Button variant="outline" size="sm" disabled={!canEdit || wantsAuto} onClick={() => setDraft('automatic')} title={`Every workspace has an automatic icon (this one: ${iconWords(auto)})`}>
          Use the automatic icon
        </Button>
        {!canEdit && <OwnerNote id="ws-icon-why">Only owners can save.</OwnerNote>}
      </div>
    </div>
  )
}
