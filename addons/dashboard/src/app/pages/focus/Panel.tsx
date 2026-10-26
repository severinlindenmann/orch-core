import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Changes } from './panel/Changes'
import { Artifacts } from './panel/Artifacts'
import { Decisions } from './panel/Decisions'
import { Context, useContextPages } from './panel/Context'
import { Research, useSources } from './panel/Research'
import { decisionGates, useChanges, usePanelTicket } from './panel/data'

const tabs = { Changes, Artifacts, Decisions, Research, Context }
export type PanelTab = keyof typeof tabs

/** A scratch session has no ticket: Changes and Decisions are hidden (P2-6). */
const SCRATCH_HIDDEN = ['Changes', 'Decisions']

export function Panel({ ticket, scratch, tab, onTab }: { ticket?: string; scratch?: boolean; tab?: PanelTab; onTab?: (tab: PanelTab) => void }) {
  const { data } = usePanelTicket(ticket)
  const changes = useChanges(ticket)
  const wiki = useContextPages(ticket)
  const research = useSources(ticket).sources.data
  const counts: Record<string, number> = { Changes: changes.data?.files.length ?? 0, Artifacts: data?.artifacts.length ?? 0, Decisions: (data?.questions_state.length ?? 0) + decisionGates(data).length, Context: (data?.acceptance_state.length ?? 0) + wiki.pages.length, Research: research?.available ? research.sources.length : 0 }
  const shown = Object.keys(tabs).filter((name) => !(scratch && SCRATCH_HIDDEN.includes(name)))
  const value = tab && shown.includes(tab) ? tab : shown[0]
  return (
    <Tabs value={value} onValueChange={(next) => onTab?.(next as PanelTab)} data-testid="focus-panel" className="min-h-0 min-w-0 flex-1 gap-0 overflow-hidden">
      <TabsList aria-label="Session details" variant="line" className="h-11 w-full shrink-0 flex-nowrap justify-start gap-0 overflow-x-auto rounded-none border-b border-border p-0 group-data-[orientation=horizontal]/tabs:h-11">
        {shown.map((name) => <TabsTrigger key={name} value={name} aria-label={name} className="h-11 flex-none whitespace-nowrap rounded-none px-2 text-xs after:!bottom-0">{name}{counts[name] > 0 && <span className="ml-1 rounded bg-surface-3 px-1 text-[10px] text-text-muted" aria-label={`${counts[name]} items`}>{counts[name]}</span>}</TabsTrigger>)}
      </TabsList>
      {shown.map((name) => [name, tabs[name as PanelTab]] as const).map(([name, Body]) => <TabsContent key={`${ticket ?? 'scratch'}:${name}`} value={name} className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 pb-6"><Body ticket={ticket} /></TabsContent>)}
    </Tabs>
  )
}
