import { Link } from '@tanstack/react-router'
import { addonActive } from '@/api/addons'
import { can } from '@/api/permissions'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { PreviewChip } from '@/addon-ui/PreviewChip'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddons, useAddonStateEntries, selectContributions } from '@/addon-ui/slots'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../workspace'
import { useRole } from '../useRole'
import { usePageHeader } from '../shell/ShellUi'

/** Renders the `nav` contribution `page` of addon `name` (declarative, inside the addon frame). */
export function AddonPage({ name, page }: { name: string; page: string }) {
  const { data: addons, isLoading } = useAddons()
  const { workspace } = useWorkspace()
  const { [name]: state } = useAddonStateEntries(workspace?.id, addonActive(workspace, name) ? [name] : [])
  const addon = state?.data
  const role = useRole()
  // Until the addon's state has loaded the page body waits (skeleton, or an error with Retry): bindings need it.
  const c = selectContributions(addons ?? [], 'nav', { workspace, addon }, state?.waiting).find((x) => x.addon === name && x.id === page)
  usePageHeader(c ? c.title : name)

  if (isLoading || !workspace) return <Skeleton className="h-40 w-full max-w-3xl" />
  if (!addonActive(workspace, name)) {
    const title = addons?.find((x) => x.name === name)?.title ?? name
    const isOwner = can(role, 'addon.manage')
    return (
      <div className="space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        <p className="text-sm text-text-muted">{title} is not enabled in {workspace.name}.</p>
        {isOwner ? (
          <Link to="/settings/$tab" params={{ tab: 'addons' }} className="text-sm text-brand hover:underline">
            Open the addon manager
          </Link>
        ) : (
          <p className="text-sm text-text-faint">Ask an owner to enable it.</p>
        )}
      </div>
    )
  }
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
        <PreviewChip name={c.addon} />
      </h1>
      <AddonContributionView c={c} ctx={{ workspace, addon }} readOnly={!can(role, 'addon.action')} />
    </div>
  )
}
