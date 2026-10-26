import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Changes } from './panel/Changes'
import { Artifacts } from './panel/Artifacts'
import { Decisions } from './panel/Decisions'
import { Context, useContextPages } from './panel/Context'
import { decisionGates, useChanges, usePanelTicket } from './panel/data'

const tabs = { Changes, Artifacts, Decisions, Context }

export function Panel({ ticket }: { ticket?: string }) {
  const { data } = usePanelTicket(ticket)
  const changes = useChanges(ticket)
  const wiki = useContextPages(ticket)
  const counts: Record<string, number> = { Changes: changes.data?.files.length ?? 0, Artifacts: data?.artifacts.length ?? 0, Decisions: (data?.questions_state.length ?? 0) + decisionGates(data).length, Context: (data?.acceptance_state.length ?? 0) + wiki.pages.length }
  return (
    <Tabs defaultValue="Changes" data-testid="focus-panel" className="min-w-0">
      <TabsList aria-label="Session details" variant="line" className="w-full flex-wrap border-b border-border">
        {Object.keys(tabs).map((tab) => <TabsTrigger key={tab} value={tab} aria-label={tab}>{tab}<span className="ml-1 rounded bg-surface-3 px-1.5 text-[10px] text-text-muted" aria-label={`${counts[tab]} items`}>{counts[tab]}</span></TabsTrigger>)}
      </TabsList>
      {Object.entries(tabs).map(([tab, Body]) => <TabsContent key={`${ticket ?? 'scratch'}:${tab}`} value={tab} className="min-w-0 p-4"><Body ticket={ticket} /></TabsContent>)}
    </Tabs>
  )
}
