import { AddonContributionView } from '@/addon-ui/AddonSlot'
import { useAddonStates, useAddons, useSlot } from '@/addon-ui/slots'
import { addonActive } from '@/api/addons'
import type { Workspace } from '@/api/types'

/** /settings/addon/$name: the addon's `settings` contribution (a form node) in its frame; read-only unless owner. */
export function AddonSettings({ name, workspace, canEdit }: { name: string; workspace: Workspace; canEdit: boolean }) {
  const { data: addons = [], isLoading } = useAddons()
  const contributions = useSlot('settings')
  const manifest = addons.find((a) => a.name === name)
  const c = contributions.find((x) => x.addon === name)
  const active = addonActive(workspace, name)
  // Wait for the saved values: the form is keyed on its data and must not remount under the user's hands.
  const [state] = Object.values(useAddonStates(workspace.id, active ? [name] : []))
  const title = manifest?.title ?? name

  if (isLoading) return null
  if (!manifest) return <p className="text-sm text-text-muted">No addon named {name} is installed.</p>
  if (!active) return <p className="text-sm text-text-muted">Enable {title} to change its settings.</p>
  if (!state) return null
  if (!c) return <p className="text-sm text-text-muted">{title} has no settings.</p>
  return <AddonContributionView c={c} ctx={{ workspace }} readOnly={!canEdit} />
}
