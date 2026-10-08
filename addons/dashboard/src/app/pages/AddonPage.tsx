import { AddonBadge } from '@/addon-ui/AddonBadge'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddons, useAddonStates, selectContributions } from '@/addon-ui/slots'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../workspace'
import { usePageHeader } from '../shell/ShellUi'

/** Renders the `nav` contribution `page` of addon `name` (declarative, inside the addon frame). */
export function AddonPage({ name, page }: { name: string; page: string }) {
  const { data: addons, isLoading } = useAddons()
  const { workspace } = useWorkspace()
  const { [name]: addon } = useAddonStates(workspace?.id, [name])
  const c = selectContributions(addons ?? [], 'nav', { workspace, addon }).find((x) => x.addon === name && x.id === page)
  usePageHeader(c ? c.title : name)

  if (isLoading) return <Skeleton className="h-40 w-full max-w-3xl" />
  if (!c)
    return (
      <div className="space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Page not found</h1>
        <p className="text-sm text-text-muted">
          The addon <span className="font-mono">{name}</span> is not enabled in this workspace or has no page <span className="font-mono">{page}</span>.
        </p>
      </div>
    )
  return (
    <div className="max-w-4xl space-y-4">
      <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
        <AddonBadge name={c.addon} className="size-5 text-xs" />
        {c.title}
      </h1>
      <AddonContributionView c={c} ctx={{ workspace, addon }} />
    </div>
  )
}
