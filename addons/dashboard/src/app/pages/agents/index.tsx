import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { useState } from 'react'
import { activeGrantOf } from '@/api/grants'
import { can, canRevokeGrant, roleOf } from '@/api/permissions'
import type { GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { AgentsSkeleton } from '../skeletons'
import { LoadFailed } from '@/components/LoadFailed'
import { useLoadFailure } from '../../useLoadFailure'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { Section } from '../ticket/shared'
import { groupOf, type SessionGroupId, useAttention } from '../../attention'
import { AgentActivity } from './AgentActivity'
import { GrantDialog, useSignGrant, type GrantAction } from './GrantDialog'
import { Grants } from './Grants'
import { SessionGroup, type SessionContext } from './Sessions'
import { fmtClock, fmtDateTime } from '@/lib/time'
import { queries } from '@/api/queries'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { validateAgentsSearch, type AgentsTab } from '../../search'
import { Pill } from '../ticket/shared'
import { useMandatesPreview } from '../../mandates/shared'
import { MandatesTab } from './Mandates'

export function AgentsPage() {
  usePageHeader('Agents')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const me = useQuery(queries.me())
  const today = useQuery({ ...queries.today(ws!), enabled: !!ws })
  const sessions = useQuery({ ...queries.agents(ws!), enabled: !!ws })
  const grants = useQuery({ ...queries.grants(ws!), enabled: !!ws })
  const attention = useAttention(ws)
  const tickets = useQuery({ ...queries.ticketsAll(ws!), enabled: !!ws })
  const activity = useQuery({ ...queries.agentActivity(ws!), enabled: !!ws })
  const [action, setAction] = useState<GrantAction | null>(null)
  const sign = useSignGrant(ws ?? '')
  // The tab is in the address (`?tab=mandates`; Sessions when absent). Mandates is a PREVIEW (concept-mandates.md).
  const tab: AgentsTab = validateAgentsSearch(useSearch({ strict: false })).tab ?? 'sessions'
  const navigate = useNavigate()
  const setTab = (next: string) => void navigate({ to: '/agents', search: next === 'mandates' ? { tab: 'mandates' } : {}, replace: true })
  const mandates = useMandatesPreview(ws)

  const failure = useLoadFailure(today, sessions, grants, activity)
  if (failure.failed) return <LoadFailed what="agents" onRetry={failure.retry} />
  if (!ws || !me.data || !today.data || !sessions.data || !grants.data || !activity.data || (tab === 'mandates' && mandates.isPending)) {
    return <AgentsSkeleton inPage tab={tab} />
  }

  const now = today.data.now
  const viewer = me.data.person
  const role = roleOf(workspace, viewer)
  const canAct = can(role, 'grant.issue')
  const name = (id: string) => (workspace ? (workspace.members.find((m) => m.person === id)?.name ?? id) : '…')
  const canRevoke = (g: GrantInfo) => canRevokeGrant(role, g.person, viewer)

  const { sessions: n, waitingOnYou, stopped } = attention.agents
  const mine = activeGrantOf(grants.data, viewer, Date.parse(now))
  const summary = [`${n} agent session${n === 1 ? '' : 's'}`, ...(canAct || waitingOnYou > 0 ? [`${waitingOnYou} waiting on you`] : []), ...(stopped > 0 ? [`${stopped} stopped`] : []), ...(mine ? [`your grant until ${mine.until.slice(0, 10) === now.slice(0, 10) ? fmtClock(mine.until) : fmtDateTime(mine.until)}`] : [])].join(' · ')
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
          {/* The sessions summary and Issue grant belong to the Sessions tab (Mandates is a preview of its own). */}
          {tab === 'sessions' && <p className="mt-1 text-[13px] text-text-muted">{summary}</p>}
        </div>
        {canAct && tab === 'sessions' && <Button onClick={() => setAction({ kind: 'issue' })}>Issue grant…</Button>}
      </div>

      <Tabs value={tab} onValueChange={setTab} className="gap-4">
        <TabsList variant="line" className="h-9 w-full min-w-0 justify-start gap-1 overflow-x-auto overflow-y-hidden border-b border-border [&>*]:flex-none">
          <TabsTrigger value="sessions">Sessions</TabsTrigger>
          <TabsTrigger value="mandates">
            Mandates <Pill className="h-4 px-1.5 text-[10px]">Preview</Pill>
          </TabsTrigger>
        </TabsList>
        <TabsContent value="sessions" className="space-y-5">
          <div className="space-y-3">
            {waiting.length > 0 && <SessionGroup id="waiting-h" title="Waiting on you" list={waiting} ctx={ctx} />}
            {group('waiting-others').length > 0 && <SessionGroup id="waiting-others-h" title="Waiting on others" list={group('waiting-others')} ctx={ctx} />}
            {group('idle').length > 0 && <SessionGroup id="idle-h" title="Idle" list={group('idle')} ctx={ctx} />}
            <SessionGroup id="working-h" title="Working" list={working} ctx={ctx} empty="No agent session is working." />
            {stoppedList.length > 0 && <SessionGroup id="stopped-h" title="Stopped" list={stoppedList} ctx={ctx} defaultOpen={false} />}
          </div>
          <Section title="Grants" className="[&>div]:p-0">
            <Grants grants={grants.data} now={now} name={name} canRevoke={canRevoke} onRevoke={(grant) => setAction({ kind: 'revoke', grant })} />
          </Section>
          <AgentActivity items={activity.data} sessions={sessions.data} />
        </TabsContent>
        <TabsContent value="mandates">
          {mandates.data ? (
            <MandatesTab ws={ws} state={mandates.data} now={now} />
          ) : (
            // A host without the preview endpoint: say so, never "Agents could not load".
            <p className="text-[13px] text-text-muted">The mandates preview is not available here.</p>
          )}
        </TabsContent>
      </Tabs>

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
