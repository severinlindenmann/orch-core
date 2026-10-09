import { useState, type ReactNode } from 'react'
import { Maximize2, TriangleAlert } from 'lucide-react'
import { findTemplate, frameDocument, templateDigest } from '@/api/widgetTemplates'
import { sha256Hex } from '@/api/sha256'
import type { TicketDocument } from '@/api/types'
import { AddonBadge, AddonUnavailable } from '@/addon-ui'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { FrameNode } from '@/addon-ui/FrameNode'
import { frameNode } from '@/addon-ui/nodes'
import { addonHairline } from '@/addon-ui/addonClasses'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { CoreWidget, widgetText } from './CoreWidget'
import type { Block, WidgetSpec } from './parse'

/** The fixed height of a widget's body on the page; "Expand" shows it larger. */
export const BODY_HEIGHT = 280

/** `reason` is the technical text (behind "Details"); `plain` says what happened and who fixes it. */
type Resolved = { ok: true; html: string; height: number; layerLabel: string; text?: string } | { ok: false; reason: string; plain: string }

const DRIFT = 'This preview changed after it was pinned, so it is not shown. Ask the agent that wrote it to update the pin.'

/** Decides what a template or one-off block may show: the pin must match the bytes, else it is code with a reason. */
function resolveFrame(spec: WidgetSpec, ticket: Pick<TicketDocument, 'key' | 'artifacts'>): Resolved {
  if (spec.layer === 'widget') {
    const t = findTemplate(spec.widget!)
    if (!t) return { ok: false, reason: `unknown widget template "${spec.widget}"`, plain: 'This widget uses a template orch does not know. Ask its author to fix the block.' }
    const now = templateDigest(t)
    if (now !== spec.sha256) return { ok: false, reason: `Drift: ${spec.widget} no longer matches this block's pin (the template is now ${now.slice(0, 12)}…, the block pins ${spec.sha256!.slice(0, 12)}…). It is not shown until the block is re-pinned.`, plain: DRIFT }
    return { ok: true, html: frameDocument(t.html, spec.data), height: spec.height ?? t.minHeight, layerLabel: spec.widget! }
  }
  if (spec.artifactTicket && spec.artifactTicket !== ticket.key)
    return { ok: false, reason: `artifact belongs to ${spec.artifactTicket}, not to this ticket`, plain: `This widget shows a page from ${spec.artifactTicket}, not from this ticket. Ask its author to fix the block.` }
  const a = ticket.artifacts.find((x) => x.name === spec.artifact)
  if (!a || a.preview === undefined) return { ok: false, reason: `artifact "${spec.artifact}" is missing from this ticket`, plain: 'The page this widget shows is not attached to this ticket. Ask the agent that wrote it to attach it again.' }
  const now = sha256Hex(a.preview)
  if (now !== spec.sha256)
    return { ok: false, reason: `sha256 does not match: ${spec.artifact} has ${now.slice(0, 12)}…, the block pins ${spec.sha256!.slice(0, 12)}…. The page changed since this widget was written, so it does not run.`, plain: DRIFT }
  return { ok: true, html: frameDocument(a.preview, spec.data), height: spec.height ?? 240, layerLabel: 'one-off' }
}

/** What a parse refusal means for the reader and who fixes it. The technical text stays behind "Details". */
export function plainReason(reason: string, sectionLabel: string): string {
  let m: RegExpMatchArray | null
  if ((m = reason.match(/unknown key "([^"]+)"/))) return `This widget uses a setting orch does not know ("${m[1]}"). Ask its author to fix the block.`
  if ((m = reason.match(/^widgets are not drawn in (.+?) \(/))) return `Widgets are not shown in ${m[1]}, because approvals sign that text.`
  if ((m = reason.match(/^id "([^"]+)" is used by more than one widget/))) return `Two widgets use the id "${m[1]}". Ask its author to rename one.`
  if ((m = reason.match(/^only the first (\d+) widgets/))) return `Only the first ${m[1]} widgets of a ticket are shown. This one is not.`
  if (/unknown widget (template|type)/.test(reason)) return 'This widget uses a kind orch does not know. Ask its author to fix the block.'
  return `This widget block in ${sectionLabel} could not be read. Ask its author to fix the block.`
}

const keyOf = (b: Block) => b.spec?.id ?? `${b.section}-${b.line}`

/** A block that cannot be drawn: what happened in plain words, then "Details" with the technical text and the block. */
function Refused({ block, reason, plain, sectionLabel }: { block: Block; reason: string; plain: string; sectionLabel: string }) {
  const [details, setDetails] = useState(false)
  const [shown, setShown] = useState(false)
  return (
    <figure data-widget={keyOf(block)} data-state="refused" className="my-3 rounded-lg border border-warning/40 bg-surface">
      <div data-widget-error role="alert" className="flex items-start gap-2 px-3 py-2 text-[13px] text-text">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
        <span>
          <span className="font-medium">
            {sectionLabel}, line {block.line}:
          </span>{' '}
          {plain}
        </span>
      </div>
      <div className="px-3 pb-2">
        <button type="button" aria-expanded={details} onClick={() => setDetails(!details)} className="text-[11px] text-text-muted underline-offset-2 hover:text-text hover:underline">
          Details
        </button>
        {details && (
          <div data-widget-details className="mt-1 space-y-1 text-[11px] text-text-muted">
            <p className="break-words font-mono">{reason}</p>
            <button type="button" aria-expanded={shown} onClick={() => setShown(!shown)} className="underline-offset-2 hover:text-text hover:underline">
              {shown ? 'Hide block' : 'Show block'}
            </button>
            {shown && (
              <div className="max-h-48 overflow-auto [&_pre]:text-[11px]">
                <CodeBlock language="json" text={block.raw} />
              </div>
            )}
          </div>
        )}
      </div>
    </figure>
  )
}

export function WidgetBlock({
  block,
  ticket,
  agentHtml,
  sectionLabel,
}: {
  block: Block
  ticket: Pick<TicketDocument, 'key' | 'artifacts'>
  /** The widgets addon is active in the ticket's workspace: frames may run. */
  agentHtml: boolean
  sectionLabel: string
}) {
  const spec = block.spec
  if (!spec || block.reason) {
    const reason = block.reason ?? 'not a widget'
    return <Refused block={block} reason={reason} plain={plainReason(reason, sectionLabel)} sectionLabel={sectionLabel} />
  }
  const framed = spec.layer !== 'type'
  // The verdict on the pin comes first, also when agent HTML is off: a refused block is never shown as merely "off".
  const res = framed ? resolveFrame(spec, ticket) : undefined
  if (res && !res.ok) return <Refused block={block} reason={res.reason} plain={res.plain} sectionLabel={sectionLabel} />
  return <Drawn block={block} spec={spec} res={res?.ok ? res : undefined} agentHtml={agentHtml} />
}

/** The widget's content. The frame is validated like any addon frame node and keeps `sandbox="allow-scripts"` only. */
function Body({ spec, res, agentHtml, height }: { spec: WidgetSpec; res: Extract<Resolved, { ok: true }> | undefined; agentHtml: boolean; height: number }): ReactNode {
  const framed = spec.layer !== 'type'
  if (framed && !agentHtml)
    return <p className="rounded-md border border-dashed border-border px-3 py-2 text-[12px] text-text-muted">Agent HTML is off in this workspace, so this widget is not drawn. {spec.caption ?? 'No text alternative given.'}</p>
  if (framed && res) {
    const node = frameNode.safeParse({ type: 'frame', title: `Sandboxed preview · ${res.layerLabel}`, html: res.html, height: Math.min(1200, Math.max(80, height)) })
    return node.success ? <FrameNode node={node.data} fallback={<AddonUnavailable addon="widgets" />} /> : <AddonUnavailable addon="widgets" />
  }
  return <CoreWidget spec={spec} />
}

/** One card per widget: a hairline (orange only when agent HTML draws it), a one-line header, a fixed-height body. */
function Drawn({ block, spec, res, agentHtml }: { block: Block; spec: WidgetSpec; res: Extract<Resolved, { ok: true }> | undefined; agentHtml: boolean }) {
  const [text, setText] = useState(false)
  const [big, setBig] = useState(false)
  const framed = spec.layer !== 'type'
  const title = spec.title ?? (framed ? (spec.widget ?? spec.artifact ?? 'Widget') : (spec.type ?? 'Widget'))
  const alt = framed ? (spec.caption ?? 'No text alternative given.') : widgetText(spec)
  const meta = (
    <>
      {spec.source && <div>Source: {spec.source}</div>}
      {spec.caption && <div>{spec.caption}</div>}
    </>
  )
  return (
    <figure data-widget={keyOf(block)} data-layer={framed ? spec.layer : 'core'} data-addon={framed ? 'widgets' : undefined} className={cn('my-3 rounded-lg border bg-surface', framed ? addonHairline : 'border-border')}>
      <figcaption className="flex items-center gap-2 px-3 py-1.5">
        {framed && <AddonBadge name="widgets" />}
        <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">
          {title}
          {framed && <span className="font-normal text-text-muted"> · Sandboxed</span>}
        </h3>
        <button type="button" aria-pressed={text} onClick={() => setText(!text)} className="rounded px-1.5 py-0.5 text-[11px] text-text-muted hover:bg-surface-2 hover:text-text">
          {text ? 'Hide text' : 'Show text'}
        </button>
        <button type="button" aria-label={`Expand ${title}`} onClick={() => setBig(true)} className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-text-muted hover:bg-surface-2 hover:text-text">
          <Maximize2 className="size-3" aria-hidden />
          Expand
        </button>
      </figcaption>
      <div className="space-y-1.5 px-3 pb-2.5">
        <div data-widget-body className={cn('overflow-auto', framed ? 'h-[280px]' : 'max-h-[280px]')}>
          {text ? <pre className="whitespace-pre-wrap rounded-md border border-border bg-bg p-2 text-[12px] text-text">{alt}</pre> : <Body spec={spec} res={res} agentHtml={agentHtml} height={BODY_HEIGHT} />}
        </div>
        {(spec.source || spec.caption) && <div className={cn('text-[11px] text-text-muted', !agentHtml && framed && 'hidden')}>{meta}</div>}
      </div>
      <Sheet open={big} onOpenChange={setBig}>
        <SheetContent side="right" className="w-[900px] max-w-[96vw] gap-0 border-border bg-surface sm:max-w-[900px]">
          <SheetHeader className="border-b border-border">
            <SheetTitle className="flex items-center gap-2 text-[14px]">
              {framed && <AddonBadge name="widgets" />}
              {title}
              {framed && <span className="font-normal text-text-muted"> · Sandboxed</span>}
            </SheetTitle>
            <SheetDescription>{framed ? 'Agent HTML in a sandboxed frame.' : 'Drawn by core.'}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-2 overflow-auto p-4">
            <Body spec={spec} res={res} agentHtml={agentHtml} height={720} />
            <div className="text-[12px] text-text-muted">{meta}</div>
          </div>
        </SheetContent>
      </Sheet>
    </figure>
  )
}
