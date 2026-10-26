import { useMemo, useState } from 'react'
import { Check, ChevronDown, Copy } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { useWorkspace } from '@/app/workspace'
import { cn } from '@/lib/utils'
import { parseBlock, type Block } from './parse'
import { WidgetBlock } from './WidgetBlock'

// The `widget` addon node: one ticket widget block drawn by core outside a ticket (the widgets gallery). The block is
// read by the same strict parser as ticket text and drawn by the same WidgetBlock card. Templates run in the sandboxed
// frame only when the node comes from the widgets addon itself AND that addon is active in this workspace: another
// addon's widget node draws core types only. No ticket: an `html` block has no artifact to show.

const NO_TICKET = { key: '', artifacts: [] }

export default function WidgetNode({ block, source, addon }: { block: string; source: boolean; addon: string }) {
  const { workspace } = useWorkspace()
  const agentHtml = addon === 'widgets' && addonActive(workspace, 'widgets')
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

/** The block's source with Copy. Beside the example from 1280 px; below that it is folded behind "Show source". */
function SourceBlock({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [open, setOpen] = useState(false)
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
    <div data-widget-source className="mb-3 min-w-0 rounded-lg border border-border bg-surface xl:my-3">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="inline-flex flex-1 items-center gap-1 text-left text-[12px] font-medium text-text-muted hover:text-text xl:hidden">
          <ChevronDown className={cn('size-3 transition-transform', !open && '-rotate-90')} aria-hidden />
          {open ? 'Hide source' : 'Show source'}
        </button>
        <span className="hidden flex-1 text-[12px] font-medium text-text-muted xl:inline">Source</span>
        <span aria-live="polite" className="text-[11px] text-text-muted">
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy' : ''}
        </span>
        <button type="button" onClick={copy} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-text-muted hover:bg-surface-2 hover:text-text">
          {state === 'copied' ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
          Copy source
        </button>
      </div>
      <div data-source-body className={cn('max-h-[300px] overflow-auto px-3 pb-2.5 [&_pre]:text-[11px] [&_pre]:leading-4', open ? 'block' : 'hidden xl:block')}>
        <CodeBlock language="markdown" text={text} />
      </div>
    </div>
  )
}

/** The `widget-index` node: chips per group that scroll to a widget on this page. */
export function WidgetIndex({ groups }: { groups: { label: string; items: { label: string; widget: string }[] }[] }) {
  const jump = (id: string) => {
    // The node schema limits ids to [a-z][a-z0-9-]{0,39}, so the selector needs no escaping.
    document.querySelector(`[data-widget="${id}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
  return (
    <nav aria-label="Jump to a widget" className="space-y-1.5 rounded-lg border border-border bg-surface px-3 py-2">
      {groups.map((g) => (
        <div key={g.label} className="flex flex-wrap items-baseline gap-1.5 text-[12px]">
          <span className="w-20 shrink-0 font-medium text-text-muted">{g.label}</span>
          {g.items.map((it) => (
            <button key={it.widget} type="button" onClick={() => jump(it.widget)} className="rounded-full border border-border px-2 py-0.5 font-mono text-[11px] text-text hover:bg-surface-2">
              {it.label}
            </button>
          ))}
        </div>
      ))}
    </nav>
  )
}
