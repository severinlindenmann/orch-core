import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Changes } from './panel/Changes'
import { Artifacts } from './panel/Artifacts'
import { Decisions } from './panel/Decisions'
import { Context } from './panel/Context'

const tabs = { Changes, Artifacts, Decisions, Context }

export function Panel() {
  return (
    <Tabs defaultValue="Changes" data-testid="focus-panel" className="min-w-0">
      <TabsList aria-label="Session details" variant="line" className="w-full flex-wrap border-b border-border">
        {Object.keys(tabs).map((tab) => <TabsTrigger key={tab} value={tab}>{tab}</TabsTrigger>)}
      </TabsList>
      {Object.entries(tabs).map(([tab, Body]) => <TabsContent key={tab} value={tab}><Body /></TabsContent>)}
    </Tabs>
  )
}
