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
  if (workspace.icon_known === false) return null
  const saved = workspaceIconOf(workspace)
  const auto = defaultWorkspaceIcon(workspace.id)
  const [draft, setDraft] = useState<WorkspaceIconChoice | null>(null)
  const shown = draft ?? saved
  const changed = draft !== null && (draft.icon !== saved.icon || draft.colour !== saved.colour)
  const custom = !!workspace.icon
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <WorkspaceIcon {...shown} />
        <p className="min-w-0 flex-1 text-[13px] text-text-muted">Shown in the left menu next to the prefix and name, and on its own when the menu is narrow. Everyone in the workspace sees it.</p>
        <Button disabled={!canEdit || !changed} onClick={() => draft && onSave(draft)} aria-describedby={canEdit ? undefined : 'ws-icon-why'}>
          Save
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!canEdit || (!custom && !changed)}
          onClick={() => (custom ? onSave(null) : setDraft(null))}
          title={`The automatic icon is ${iconWords(auto)}`}
        >
          Use the automatic icon
        </Button>
      </div>
      {!canEdit && <OwnerNote id="ws-icon-why">Only owners can save.</OwnerNote>}
      <div role="group" aria-label="Icon" className="flex flex-wrap gap-1.5">
        {WORKSPACE_ICONS.map((i) => (
          <button
            key={i.id}
            type="button"
            aria-label={i.label}
            aria-pressed={shown.icon === i.id}
            disabled={!canEdit}
            onClick={() => setDraft({ ...shown, icon: i.id })}
            className={cn('rounded-md p-0.5 outline-none focus-visible:ring-2 focus-visible:ring-brand disabled:opacity-60', shown.icon === i.id ? 'ring-2 ring-brand' : 'hover:bg-surface-2')}
          >
            <WorkspaceIcon icon={i.id} colour={shown.colour} />
          </button>
        ))}
      </div>
      <div role="group" aria-label="Colour" className="flex flex-wrap gap-1.5">
        {WORKSPACE_COLOURS.map((c) => (
          <button
            key={c.id}
            type="button"
            aria-label={c.label}
            aria-pressed={shown.colour === c.id}
            disabled={!canEdit}
            onClick={() => setDraft({ ...shown, colour: c.id })}
            style={{ background: c.value }}
            className={cn('flex size-7 items-center justify-center rounded-full text-white outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2 disabled:opacity-60', shown.colour === c.id && 'ring-2 ring-text ring-offset-2')}
          >
            {shown.colour === c.id && <Check aria-hidden className="size-3.5" />}
          </button>
        ))}
      </div>
    </div>
  )
}
