// Schedules on a real host (#720): the host builds follow-ups so far (an agent schedules its own next session), so the page
// has one list, with the same tab the mock's Schedules page draws.
import { usePageHeader } from '../../shell/ShellUi'
import FollowupsPanel from './FollowupsPanel'
import { useFollowups } from './FollowupParts'

export function SchedulesPage() {
  usePageHeader('Schedules')
  const { rows } = useFollowups()
  const open = rows.filter((r) => r.state === 'pending' || r.state === 'needs_person').length
  return (
    <div className="w-full min-w-0 max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Schedules</h1>
      <div role="tablist" aria-label="Schedules" className="flex gap-1 border-b border-border">
        <span role="tab" aria-selected="true" className="-mb-px border-b-2 border-brand px-2 py-1.5 text-[13px] font-medium">
          Follow-ups <span className="font-mono text-[11px] text-text-faint">{open}</span>
        </span>
      </div>
      <FollowupsPanel />
    </div>
  )
}
