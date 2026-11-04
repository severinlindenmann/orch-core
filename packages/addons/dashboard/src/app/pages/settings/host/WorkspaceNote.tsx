// One quiet line in Settings > General: whether Claude Code trusts this workspace's folders and whether the history
// of the person's work is on. No switches (owner, run 13): the setup turns both on, and the small "i" says what they are.
import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { queries } from '@/api/queries'
import { showsLogout } from '@/api/features'
import type { Workspace } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useViewerRole } from './useViewerRole'

const TRUST_ON = "Claude Code asks you to trust every folder it works in. Orch's agents start in this workspace's ticket folders without that question, only in folders orch makes."
const TRUST_OFF = 'Claude Code asks you to trust each ticket folder before an agent can work in it. Setup turns this off for the folders orch makes.'
const TRUST_MISSING = 'Claude Code has not been started on this computer yet. Start it once; then orch can let its agents work without the trust question.'
const HISTORY_ON = 'Orch saves your tickets and their history on this computer every 15 minutes, in the background. It is not sent anywhere.'
const HISTORY_OFF = 'Your tickets and their history are not being saved for you. Setup turns this on.'

/** A line the host cannot speak to is left out; nothing is drawn when it says neither. */
export function WorkspaceNote({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const mayRecord = useViewerRole(workspace) === 'owner' // the host checks the owner role on both routes
  // The host refuses the Claude-trust route in a hub or remote session by design: not asked there, so no refused call.
  const trust = useQuery({ ...queries.claudeTrust(ws), enabled: mayRecord && !showsLogout() })
  const records = useQuery({ ...queries.records(ws), enabled: mayRecord })
  const t = trust.data
  const r = records.data
  const parts: string[] = []
  const more: string[] = []
  if (mayRecord && t?.available) {
    const missing = t.config === 'missing'
    parts.push(missing ? 'Claude Code not set up yet' : t.on ? 'Claude Code trusts this folder' : 'Claude Code asks about each folder')
    more.push(missing ? TRUST_MISSING : t.on ? TRUST_ON : TRUST_OFF)
  }
  if (mayRecord && r?.available && r.configured !== false && r.repo) {
    const on = r.auto?.on === true
    parts.push(on ? 'History on' : 'History off')
    more.push(on ? HISTORY_ON : HISTORY_OFF)
  }
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
        <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] text-[13px]">{more.join(' ')}</PopoverContent>
      </Popover>
    </p>
  )
}
