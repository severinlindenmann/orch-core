import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { AddonPackage } from '@/api/types'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { PreviewChip } from '@/addon-ui/PreviewChip'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { CapabilityChips } from './CapabilityChips'

/** "Browse addons": what can be installed here. Install opens core's capability sheet; one signature grants and turns it on. */
export function Catalog({ ws, canEdit, open, onOpenChange, onInstall }: { ws: string; canEdit: boolean; open: boolean; onOpenChange: (o: boolean) => void; onInstall: (pkg: AddonPackage) => void }) {
  const { data, isLoading } = useQuery({ queryKey: ['addon-catalog', ws], queryFn: () => api.getAddonCatalog(ws), enabled: open })
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-[420px] gap-0 overflow-y-auto border-border bg-surface sm:max-w-[420px]">
        <SheetHeader>
          <SheetTitle>Browse addons</SheetTitle>
          <SheetDescription>Installing asks you to review what it may do, then turns it on with one signature.</SheetDescription>
        </SheetHeader>
        <div className="space-y-3 px-4 pb-4">
          {isLoading && <Skeleton className="h-24 w-full" />}
          {data?.length === 0 && <p className="text-[13px] text-text-muted">Everything in the catalog is installed.</p>}
          {data?.map((a) => (
            <article key={a.name} aria-label={a.title} className="space-y-2 rounded-lg border border-border bg-bg p-3">
              <header className="flex items-center gap-2">
                <AddonBadge name={a.name} />
                <div className="flex flex-1 items-center gap-2">
                  <h3 className="text-[14px] font-semibold">{a.title}</h3>
                  <PreviewChip name={a.name} />
                </div>
                {a.first_party && <Badge variant="outline">First-party</Badge>}
                <span className="font-mono text-[12px] text-text-muted">{a.version}</span>
              </header>
              <p className="text-[13px] text-text-muted">{a.description}</p>
              <CapabilityChips capabilities={a.capabilities} />
              <Button size="sm" disabled={!canEdit} onClick={() => onInstall(a)}>
                Install
              </Button>
            </article>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  )
}
