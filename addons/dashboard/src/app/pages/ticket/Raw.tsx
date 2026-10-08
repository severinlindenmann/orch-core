import { useMemo } from 'react'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { CopyButton } from './Header'
import { fmtBytes, type TabProps } from './shared'

export function Raw({ ticket }: TabProps) {
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
      <div className="max-h-[70vh] overflow-auto rounded-md" data-testid="raw-json">
        <CodeBlock language="json" text={json} />
      </div>
    </div>
  )
}
