import { useQuery } from '@tanstack/react-query'
import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { Skeleton } from '@/components/ui/skeleton'

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading addon settings" className="max-w-3xl space-y-3">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

/** /settings/addon/$name: the addon's `settings` contribution (a form node) in its frame; read-only unless owner. */
export function AddonSettings({ name, workspace, canEdit }: { name: string; workspace: Workspace; canEdit: boolean }) {
  const addons = useAddons()
  const contributions = useSlot('settings')
  const pkg = addons.data?.find((a) => a.name === name)
  const c = contributions.find((x) => x.addon === name)
  const active = addonActive(workspace, name)
  // Wait for the saved values: the form is keyed on its data and must not remount under the user's hands.
  const state = useQuery({
    queryKey: ['addon-state', workspace.id, name],
    queryFn: () => api.getAddonState(workspace.id, name),
    enabled: active,
    retry: false,
  })
  const title = pkg?.title ?? name
  const failed = (e: unknown) => (
    <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px]">
      Could not load the {title} settings: {e instanceof Error ? e.message : 'something went wrong'}.
    </p>
  )

  if (addons.isLoading) return <Loading />
  if (addons.error) return failed(addons.error)
  if (!pkg) return <p className="text-sm text-text-muted">No addon named {name} is installed.</p>
  if (!active) return <p className="text-sm text-text-muted">Enable {title} to change its settings.</p>
  if (state.error) return failed(state.error)
  if (!state.data) return <Loading />
  if (!c) return <p className="text-sm text-text-muted">{title} has no settings.</p>
  return <AddonContributionView c={c} ctx={{ workspace }} readOnly={!canEdit} />
}
