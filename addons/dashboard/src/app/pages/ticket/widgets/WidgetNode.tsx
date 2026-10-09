import { useMemo, useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { useWorkspace } from '@/app/workspace'
import { parseBlock, type Block } from './parse'
import { WidgetBlock } from './WidgetBlock'

// The `widget` addon node: one ticket widget block drawn by core outside a ticket (the widgets gallery). The block is
// read by the same strict parser as ticket text and drawn by the same WidgetBlock card; templates run in the sandboxed
// frame only while the widgets addon is active in this workspace. No ticket: an `html` block has no artifact to show.

const NO_TICKET = { key: '', artifacts: [] }

export default function WidgetNode({ block, source }: { block: string; source: boolean }) {
  const { workspace } = useWorkspace()
  const agentHtml = addonActive(workspace, 'widgets')
  const parsed: Block = useMemo(() => ({ section: 'example', line: 1, raw: block, ...parseBlock(block) }), [block])
  const drawn = <WidgetBlock block={parsed} ticket={NO_TICKET} agentHtml={agentHtml} sectionLabel="This example" />
  if (!source) return drawn
  return (
    <div className="grid items-start gap-x-4 xl:grid-cols-2">
      <div className="min-w-0">{drawn}</div>
      <SourceBlock text={'```orch\n' + block + '\n```'} />
    </div>
  )
}

function SourceBlock({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = () => {
    try {
      void navigator.clipboard.writeText(text).then(
        () => setState('copied'),
        () => setState('failed'),
      )
    } catch {
      setState('failed')
    }
  }
  return (
    <div data-widget-source className="my-3 min-w-0 rounded-lg border border-border bg-surface">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="flex-1 text-[12px] font-medium text-text-muted">Source</span>
        <span aria-live="polite" className="text-[11px] text-text-muted">
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy' : ''}
        </span>
        <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-text-muted hover:bg-surface-2 hover:text-text">
          {state === 'copied' ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
          Copy source
        </button>
      </div>
      <div className="max-h-[300px] overflow-auto px-3 pb-2.5 [&_pre]:text-[11px] [&_pre]:leading-4">
        <CodeBlock language="markdown" text={text} />
      </div>
    </div>
  )
}
