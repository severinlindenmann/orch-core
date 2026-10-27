// Schedules on a real host: "My schedules" (runs a person sets on a clock, U7) and Follow-ups (#720: an agent
// schedules its own next session), as two tabs.
import { useState } from 'react'
import { usePageHeader } from '../../shell/ShellUi'
import { isRealHost } from '@/api/features'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import FollowupsPanel from './FollowupsPanel'
import { useFollowups } from './FollowupParts'
import MySchedules from './MySchedules'

type Tab = 'mine' | 'followups'

export function SchedulesPage() {
  usePageHeader('Schedules')
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const me = useMe(ws).data
  const role = me?.role
  const real = isRealHost()
  const [tab, setTab] = useState<Tab>(real ? 'mine' : 'followups')
  const { rows } = useFollowups()
  const open = rows.filter((r) => r.state === 'pending' || r.state === 'needs_person').length
  const tabClass = (on: boolean) => `-mb-px px-2 py-1.5 text-[13px] font-medium ${on ? 'border-b-2 border-brand' : 'text-text-muted'}`
  return (
    <div className="w-full min-w-0 max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Schedules</h1>
      <div role="tablist" aria-label="Schedules" className="flex gap-1 border-b border-border">
        {real && <button type="button" role="tab" aria-selected={tab === 'mine'} className={tabClass(tab === 'mine')} onClick={() => setTab('mine')}>
          My schedules
        </button>}
        <button type="button" role="tab" aria-selected={tab === 'followups'} className={tabClass(tab === 'followups')} onClick={() => setTab('followups')}>
          Follow-ups <span className="font-mono text-[11px] text-text-faint">{open}</span>
        </button>
      </div>
      {real && tab === 'mine' ? <MySchedules ws={ws} manager={role === 'owner' || role === 'maintainer'} /> : <FollowupsPanel />}
    </div>
  )
}
