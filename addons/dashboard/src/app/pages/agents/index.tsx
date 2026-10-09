import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/client'
import { activeGrantOf } from '@/api/grants'
import { can, canRevokeGrant, roleOf } from '@/api/permissions'
import type { GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { Section } from '../ticket/shared'
import { groupOf, type SessionGroupId, useAttention } from '../../attention'
import { AgentActivity } from './AgentActivity'
import { GrantDialog, useSignGrant, type GrantAction } from './GrantDialog'
import { Grants } from './Grants'
import { SessionGroup, type SessionContext } from './Sessions'

export function AgentsPage() {
  usePageHeader('Agents')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const sessions = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  const attention = useAttention(ws)
  const tickets = useQuery({ queryKey: ['tickets', ws, 'all'], queryFn: () => api.listTickets(ws!), enabled: !!ws })
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
  const role = roleOf(workspace, viewer)
  const canAct = can(role, 'grant.issue')
  const name = (id: string) => (workspace ? (workspace.members.find((m) => m.person === id)?.name ?? id) : '…')
  const canRevoke = (g: GrantInfo) => canRevokeGrant(role, g.person, viewer)

  const { sessions: n, waitingOnYou, stopped } = attention.agents
  const mine = activeGrantOf(grants.data, viewer, Date.parse(now))
  const summary = [`${n} agent session${n === 1 ? '' : 's'}`, ...(canAct || waitingOnYou > 0 ? [`${waitingOnYou} waiting on you`] : []), ...(stopped > 0 ? [`${stopped} stopped`] : []), ...(mine ? [`your grant until ${mine.until.slice(11, 16)}`] : [])].join(' · ')
  const titles = new Map((tickets.data ?? []).map((t) => [t.key, t.title]))
  const ctx: SessionContext = { sessions: sessions.data, viewer, name, now, title: (k) => titles.get(k) }
  const roots = sessions.data.filter((s) => !s.parent)
  const group = (g: SessionGroupId) => roots.filter((s) => groupOf(s, sessions.data, viewer) === g)
  const waiting = group('waiting')
  const stoppedList = group('stopped')
  const working = group('working')

  return (
    <div className="max-w-[1040px] space-y-5">
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <h1 className="text-xl font-semibold tracking-tight">Agents</h1>
          <p className="mt-1 text-[13px] text-text-muted">{summary}</p>
        </div>
        {canAct && <Button onClick={() => setAction({ kind: 'issue' })}>Issue grant…</Button>}
      </div>

      <div className="space-y-3">
        {waiting.length > 0 && <SessionGroup id="waiting-h" title="Waiting on you" list={waiting} ctx={ctx} />}
        <SessionGroup id="working-h" title="Working" list={working} ctx={ctx} empty="No agent session is working." />
        {stoppedList.length > 0 && <SessionGroup id="stopped-h" title="Stopped" list={stoppedList} ctx={ctx} defaultOpen={false} />}
      </div>
      <Section title="Grants" className="[&>div]:p-0">
        <Grants grants={grants.data} now={now} name={name} canRevoke={canRevoke} onRevoke={(grant) => setAction({ kind: 'revoke', grant })} />
      </Section>
      <AgentActivity items={activity.data} sessions={sessions.data} />

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
