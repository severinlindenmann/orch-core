import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/client'
import type { GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { Section } from '../ticket/shared'
import { AgentActivity } from './AgentActivity'
import { GrantDialog, useSignGrant, type GrantAction } from './GrantDialog'
import { Grants, grantState } from './Grants'
import { Sessions } from './Sessions'

export function AgentsPage() {
  usePageHeader('Agents')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const sessions = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  const activity = useQuery({ queryKey: ['agent-activity', ws], queryFn: () => api.getAgentActivity(ws!), enabled: !!ws })
  const [action, setAction] = useState<GrantAction | null>(null)
  const sign = useSignGrant(ws ?? '')

  if (!ws || !me.data || !today.data || !sessions.data || !grants.data || !activity.data) {
    return (
      <div className="space-y-4" aria-busy="true">
        <h1 className="text-xl font-semibold tracking-tight">Agents</h1>
        <Skeleton className="h-5 w-96" />
        <Skeleton className="h-40 w-full max-w-4xl" />
      </div>
    )
  }

  const now = today.data.now
  const viewer = me.data.person
  const role = workspace?.members.find((m) => m.person === viewer)?.role
  const canAct = role === 'owner' || role === 'maintainer'
  const name = (id: string) => workspace?.members.find((m) => m.person === id)?.name ?? id
  const canRevoke = (g: GrantInfo) => canAct && (role === 'owner' || g.person === viewer)

  const working = sessions.data.filter((s) => s.state === 'working').length
  const waiting = sessions.data.filter((s) => s.state === 'waiting' && s.for === viewer).length
  const mine = grants.data.find((g) => g.person === viewer && g.scope === 'all' && grantState(g, Date.parse(now)) === 'active')
  const summary = [`${working} session${working === 1 ? '' : 's'} working`, ...(canAct ? [`${waiting} waiting on you`] : []), ...(mine ? [`grant until ${mine.until.slice(11, 16)}`] : [])].join(' · ')

  return (
    <div className="max-w-5xl space-y-5">
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <h1 className="text-xl font-semibold tracking-tight">Agents</h1>
          <p className="mt-1 text-[13px] text-text-muted">{summary}</p>
        </div>
        {canAct && <Button onClick={() => setAction({ kind: 'issue' })}>Issue grant…</Button>}
      </div>

      <Section title="Sessions" className="[&>div]:p-0">
        <Sessions sessions={sessions.data} viewer={viewer} name={name} now={now} />
      </Section>
      <Section title="Grants" className="[&>div]:p-0">
        <Grants grants={grants.data} now={now} name={name} canRevoke={canRevoke} onRevoke={(grant) => setAction({ kind: 'revoke', grant })} />
      </Section>
      <Section title="Agent activity" className="[&>div]:p-0">
        <AgentActivity items={activity.data} name={name} />
      </Section>

      <GrantDialog
        action={action}
        now={now}
        onSign={(a, hours) => {
          setAction(null)
          void sign(a, hours)
        }}
        onClose={() => setAction(null)}
      />
    </div>
  )
}
