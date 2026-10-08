import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, TriangleAlert } from 'lucide-react'
import { findTemplate, frameDocument, templateDigest } from '@/api/widgetTemplates'
import { sha256Hex } from '@/api/sha256'
import type { TicketDocument } from '@/api/types'
import { AddonNode } from '@/addon-ui/AddonNode'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { cn } from '@/lib/utils'
import { Pill } from '../shared'
import { CoreWidget, widgetText } from './CoreWidget'
import type { Block, WidgetSpec } from './parse'

/** More drawn widgets than this: only the first OPEN_FIRST start open (the rest are one click away). */
export const COLLAPSE_ABOVE = 4
export const OPEN_FIRST = 2

type Resolved = { ok: true; html: string; height: number; layerLabel: string; text?: string } | { ok: false; reason: string }

/** Decides what a template or one-off block may show: the pin must match the bytes, else it is code with a reason. */
function resolveFrame(spec: WidgetSpec, ticket: Pick<TicketDocument, 'key' | 'artifacts'>): Resolved {
  if (spec.layer === 'widget') {
    const t = findTemplate(spec.widget!)
    if (!t) return { ok: false, reason: `unknown widget template "${spec.widget}"` }
    const now = templateDigest(t)
    if (now !== spec.sha256) return { ok: false, reason: `Drift: ${spec.widget} no longer matches this block's pin (the template is now ${now.slice(0, 12)}…, the block pins ${spec.sha256!.slice(0, 12)}…). It is not shown until the block is re-pinned.` }
    return { ok: true, html: frameDocument(t.html, spec.data), height: spec.height ?? t.minHeight, layerLabel: spec.widget! }
  }
  if (spec.artifactTicket && spec.artifactTicket !== ticket.key) return { ok: false, reason: `artifact belongs to ${spec.artifactTicket}, not to this ticket` }
  const a = ticket.artifacts.find((x) => x.name === spec.artifact)
  if (!a || a.preview === undefined) return { ok: false, reason: `artifact "${spec.artifact}" is missing from this ticket` }
  const now = sha256Hex(a.preview)
  if (now !== spec.sha256)
    return { ok: false, reason: `sha256 does not match: ${spec.artifact} has ${now.slice(0, 12)}…, the block pins ${spec.sha256!.slice(0, 12)}…. The page changed since this widget was written, so it does not run.` }
  return { ok: true, html: frameDocument(a.preview, spec.data), height: spec.height ?? 240, layerLabel: 'one-off' }
}

const keyOf = (b: Block) => b.spec?.id ?? `${b.section}-${b.line}`

/** A block that cannot be drawn: one line saying where and why, then the raw block as code. */
function Refused({ block, reason, sectionLabel }: { block: Block; reason: string; sectionLabel: string }) {
  return (
    <figure data-widget={keyOf(block)} data-state="refused" className="my-3 rounded-lg border border-warning/40 bg-surface">
      <div data-widget-error role="note" className="flex items-start gap-2 px-3 py-2 text-[12px] text-text">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
        <span>
          <span className="font-medium">
            {sectionLabel}, line {block.line}:
          </span>{' '}
          {reason}
        </span>
      </div>
      <div className="max-h-48 overflow-auto px-3 pb-3 [&_pre]:text-[11px]">
        <CodeBlock language="json" text={block.raw} />
      </div>
    </figure>
  )
}

export function WidgetBlock({
  block,
  ticket,
  agentHtml,
  sectionLabel,
  drawnTotal,
}: {
  block: Block
  ticket: Pick<TicketDocument, 'key' | 'artifacts'>
  /** The widgets addon is active in the ticket's workspace: frames may run. */
  agentHtml: boolean
  sectionLabel: string
  drawnTotal: number
}) {
  const spec = block.spec
  if (!spec || block.reason) return <Refused block={block} reason={block.reason ?? 'not a widget'} sectionLabel={sectionLabel} />
  const framed = spec.layer !== 'type'
  const res = framed && agentHtml ? resolveFrame(spec, ticket) : undefined
  if (res && !res.ok) return <Refused block={block} reason={res.reason} sectionLabel={sectionLabel} />
  return <Drawn block={block} spec={spec} res={res?.ok ? res : undefined} agentHtml={agentHtml} drawnTotal={drawnTotal} />
}

function Drawn({ block, spec, res, agentHtml, drawnTotal }: { block: Block; spec: WidgetSpec; res: Extract<Resolved, { ok: true }> | undefined; agentHtml: boolean; drawnTotal: number }) {
  const startOpen = drawnTotal <= COLLAPSE_ABOVE || (block.index ?? 0) < OPEN_FIRST
  const [open, setOpen] = useState(startOpen)
  const [text, setText] = useState(false)
  const framed = spec.layer !== 'type'
  const title = spec.title ?? (framed ? (spec.widget ?? spec.artifact ?? 'Widget') : (spec.type ?? 'Widget'))
  const layer = framed ? `agent HTML · ${spec.layer === 'widget' ? spec.widget : 'one-off'}` : 'core'
  const alt = framed ? (spec.caption ?? 'No text alternative given.') : widgetText(spec)
  let body: ReactNode
  if (framed && !res)
    body = <p className="rounded-md border border-dashed border-border px-3 py-2 text-[12px] text-text-muted">Agent HTML is off in this workspace, so this widget is not drawn. {spec.caption ?? 'No text alternative given.'}</p>
  else if (framed && res) body = <AddonNode addon="widgets" node={{ type: 'frame', title: `Sandboxed frame · ${res.layerLabel}`, html: res.html, height: Math.min(1200, Math.max(80, res.height)) }} />
  else body = <CoreWidget spec={spec} />
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <figure data-widget={keyOf(block)} data-layer={framed ? spec.layer : 'core'} data-open={open} className="my-3 rounded-lg border border-border bg-surface">
      <figcaption className="flex items-center gap-2 px-2 py-1.5">
        <button type="button" aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${title}`} onClick={() => setOpen(!open)} className="inline-flex size-6 items-center justify-center rounded text-text-muted hover:bg-surface-2 hover:text-text">
          <Chevron className="size-4" aria-hidden />
        </button>
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">{title}</h3>
        <Pill tone="neutral" className="font-mono text-[10px]">
          {layer}
        </Pill>
        {open && (
          <button type="button" aria-pressed={text} onClick={() => setText(!text)} className="rounded px-1.5 py-0.5 text-[11px] text-text-muted hover:bg-surface-2 hover:text-text">
            {text ? 'Hide text' : 'Show text'}
          </button>
        )}
      </figcaption>
      {open && (
        <div className="space-y-1.5 px-3 pb-2.5">
          {text ? <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-2 text-[12px] text-text">{alt}</pre> : body}
          {(spec.source || spec.caption) && (
            <div className={cn('text-[11px] text-text-muted', !agentHtml && framed && 'hidden')}>
              {spec.source && <div>Source: {spec.source}</div>}
              {spec.caption && !text && <div>{spec.caption}</div>}
            </div>
          )}
        </div>
      )}
    </figure>
  )
}
