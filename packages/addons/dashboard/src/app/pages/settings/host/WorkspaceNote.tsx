// One quiet line in Settings > General: whether Claude Code trusts this workspace's folders and whether the history
// of the person's work is on. No switches (owner, run 13): the setup turns both on, and the small "i" says what they are.
import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useViewerRole } from './useViewerRole'

const MORE =
  "Claude Code asks you to trust every folder it works in; orch's agents start in this workspace's ticket folders without that question, only for folders orch makes. The history saves your tickets on this computer every 15 minutes, in the background; it is not sent anywhere."

/** A line the host cannot speak to is left out; nothing is drawn when it says neither. */
export function WorkspaceNote({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const mayRecord = useViewerRole(workspace) === 'owner' // the host checks the owner role on both routes
  const trust = useQuery({ ...queries.claudeTrust(ws), enabled: mayRecord })
  const records = useQuery({ ...queries.records(ws), enabled: mayRecord })
  const t = trust.data
  const r = records.data
  const parts: string[] = []
  if (t?.available) parts.push(t.on ? 'Claude Code trusts this folder' : 'Claude Code asks about each folder')
  if (mayRecord && r?.available && r.configured !== false && r.repo) parts.push(r.auto?.on === true ? 'History on' : 'History off')
  if (parts.length === 0) return null
  return (
    <p className="flex items-center gap-1.5 text-[13px] text-text-muted" data-testid="workspace-note">
      <span>{parts.join(' · ')}</span>
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" aria-label="More about this" className="rounded p-0.5 text-text-faint hover:text-text-muted">
            <Info className="size-3.5" aria-hidden />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] text-[13px]">{MORE}</PopoverContent>
      </Popover>
    </p>
  )
}
