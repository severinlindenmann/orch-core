import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Changes } from './panel/Changes'
import { Artifacts } from './panel/Artifacts'
import { Decisions } from './panel/Decisions'
import { Context, useContextPages } from './panel/Context'
import { Research, useSources } from './panel/Research'
import { decisionGates, useChanges, usePanelTicket } from './panel/data'

const tabs = { Changes, Artifacts, Decisions, Context, Research }

/** A scratch session has no ticket: Changes and Decisions are hidden (P2-6). */
const SCRATCH_HIDDEN = ['Changes', 'Decisions']

export function Panel({ ticket, scratch }: { ticket?: string; scratch?: boolean }) {
  const { data } = usePanelTicket(ticket)
  const changes = useChanges(ticket)
  const wiki = useContextPages(ticket)
  const research = useSources(ticket).sources.data
  const counts: Record<string, number> = { Changes: changes.data?.files.length ?? 0, Artifacts: data?.artifacts.length ?? 0, Decisions: (data?.questions_state.length ?? 0) + decisionGates(data).length, Context: (data?.acceptance_state.length ?? 0) + wiki.pages.length, Research: research?.available ? research.sources.length : 0 }
  const shown = Object.keys(tabs).filter((tab) => !(scratch && SCRATCH_HIDDEN.includes(tab)))
  return (
    <Tabs defaultValue={shown[0]} data-testid="focus-panel" className="min-w-0">
      <TabsList aria-label="Session details" variant="line" className="w-full flex-wrap border-b border-border">
        {shown.map((tab) => <TabsTrigger key={tab} value={tab} aria-label={tab}>{tab}<span className="ml-1 rounded bg-surface-3 px-1.5 text-[10px] text-text-muted" aria-label={`${counts[tab]} items`}>{counts[tab]}</span></TabsTrigger>)}
      </TabsList>
      {shown.map((tab) => [tab, tabs[tab as keyof typeof tabs]] as const).map(([tab, Body]) => <TabsContent key={`${ticket ?? 'scratch'}:${tab}`} value={tab} className="min-w-0 p-4"><Body ticket={ticket} /></TabsContent>)}
    </Tabs>
  )
}
