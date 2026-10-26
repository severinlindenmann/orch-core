import { useParams } from '@tanstack/react-router'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { hasFeature } from '@/api/features'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'

export function FocusPage() {
  usePageHeader('Focus mode')
  const { ticket } = useParams({ strict: false })
  const { workspace } = useWorkspace()
  if (!workspace || !hasFeature('focus', workspace.id)) return null
  return (
    <div className="min-w-0 space-y-2">
      <h1 className="text-xl font-semibold tracking-tight">Focus mode</h1>
      <AddonFrame addon="focus" addonTitle="Focus mode" title="Focus mode" level={2}>
        {ticket && <p className="break-words font-mono text-sm">{ticket}</p>}
      </AddonFrame>
    </div>
  )
}
