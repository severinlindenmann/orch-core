import { Check } from 'lucide-react'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { WORKSPACE_COLOURS, WORKSPACE_ICONS, WorkspaceIcon, useWorkspaceIcon } from '@/app/workspaceIcon'

/** Picks the icon and colour shown for this workspace in the left menu. Saved on this device at once; anyone may change it for themselves. */
export function WorkspaceIconPicker({ workspace }: { workspace: Workspace }) {
  const { icon, colour, set, custom } = useWorkspaceIcon(workspace.id, workspace.name)
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <WorkspaceIcon icon={icon} colour={colour} />
        <p className="flex-1 text-[13px] text-text-muted">Shown in the left menu next to the name, and on its own when the menu is narrow.</p>
        <Button variant="outline" size="sm" disabled={!custom} onClick={() => set(null)}>
          Use the automatic icon
        </Button>
      </div>
      <div role="group" aria-label="Icon" className="flex flex-wrap gap-1.5">
        {WORKSPACE_ICONS.map((i) => (
          <button
            key={i.id}
            type="button"
            aria-label={i.label}
            aria-pressed={icon === i.id}
            onClick={() => set({ icon: i.id, colour })}
            className={cn('rounded-md p-0.5 outline-none focus-visible:ring-2 focus-visible:ring-brand', icon === i.id ? 'ring-2 ring-brand' : 'hover:bg-surface-2')}
          >
            <WorkspaceIcon icon={i.id} colour={colour} />
          </button>
        ))}
      </div>
      <div role="group" aria-label="Colour" className="flex flex-wrap gap-1.5">
        {WORKSPACE_COLOURS.map((c) => (
          <button
            key={c.id}
            type="button"
            aria-label={c.label}
            aria-pressed={colour === c.id}
            onClick={() => set({ icon, colour: c.id })}
            style={{ background: c.value }}
            className={cn('flex size-7 items-center justify-center rounded-full text-white outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2', colour === c.id && 'ring-2 ring-text ring-offset-2')}
          >
            {colour === c.id && <Check aria-hidden className="size-3.5" />}
          </button>
        ))}
      </div>
    </div>
  )
}
