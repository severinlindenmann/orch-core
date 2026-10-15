import { useMemo } from 'react'
import { addonActive } from '@/api/addons'
import { useWorkspace } from '@/app/workspace'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { CopyButton } from './Header'
import { fmtBytes, type TabProps } from './shared'

export function Raw({ ticket }: TabProps) {
  const { workspace } = useWorkspace()
  const inactive = workspace ? Object.keys(ticket.addons ?? {}).filter((n) => !addonActive(workspace, n)) : []
  const json = useMemo(() => JSON.stringify(ticket, null, 2), [ticket])
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-[12px] text-text-muted">
        <span>
          Ticket document (<span className="font-mono">orch show --json</span>) · {fmtBytes(json.length)}
        </span>
        <span className="flex-1" />
        <CopyButton text={json} label="Copy JSON" />
      </div>
      {inactive.length > 0 && (
        <p className="text-[12px] text-text-faint">
          Inactive addon data: <span className="font-mono">{inactive.join(', ')}</span> (inactive in this workspace, shown read-only).
        </p>
      )}
      <div className="max-h-[70vh] overflow-auto rounded-md" data-testid="raw-json">
        <CodeBlock language="json" text={json} />
      </div>
    </div>
  )
}
