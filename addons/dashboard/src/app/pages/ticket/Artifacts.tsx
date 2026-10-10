import { BarChart3, Check, Copy, ExternalLink, FileJson, FileText, Image as ImageIcon, Link2, ScrollText, Table2, Workflow } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { workspaceOfTicket } from '@/api/workspaces'
import { frameDocument } from '@/api/widgetTemplates'
import { FrameNode } from '@/addon-ui/FrameNode'
import { frameNode } from '@/addon-ui/nodes'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import type { Artifact } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { agentName, ago, fmtBytes, Mono, Pill, shortHash, type Jump, type TabProps } from './shared'
import { addonHairline, addonTile } from '@/addon-ui/addonClasses'

export const KIND_ICON: Record<Artifact['kind'], typeof FileText> = {
  screenshot: ImageIcon,
  log: ScrollText,
  report: FileText,
  link: Link2,
  dataset: Table2,
  build: BarChart3,
  diagram: Workflow,
  receipt: FileJson,
  feedback: FileText,
  other: FileText,
}

/** Deterministic pseudo-random numbers from the sha, so a thumbnail is stable. */
function rng(seed: string) {
  let h = 2166136261
  for (const c of seed) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0
  return () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0
    h = Math.imul(h ^ (h >>> 13), 3266489909) >>> 0
    return ((h ^= h >>> 16) >>> 0) / 4294967296
  }
}

/** Generated placeholder art. No external images are ever loaded. */
export function Thumb({ a, large = false }: { a: Artifact; large?: boolean }) {
  const r = rng(a.sha256 + a.name)
  const w = 240
  const h = 150
  let body: ReactNode
  if (a.kind === 'screenshot') {
    const bars = Array.from({ length: 7 }, (_, i) => ({ y: 44 + i * 14, w: 40 + r() * 140, ok: r() > 0.2 }))
    body = (
      <>
        <rect x="0" y="0" width={w} height="24" fill="var(--surface-3)" />
        {[0, 1, 2].map((i) => (
          <circle key={i} cx={12 + i * 12} cy="12" r="3.5" fill="var(--border-strong)" />
        ))}
        <rect x="64" y="7" width="110" height="10" rx="5" fill="var(--surface-2)" />
        <rect x="12" y="32" width="64" height="8" rx="2" fill="var(--border-strong)" />
        {bars.map((b, i) => (
          <g key={i}>
            <rect x="12" y={b.y} width="10" height="8" rx="2" fill={b.ok ? 'var(--success)' : 'var(--danger)'} opacity="0.8" />
            <rect x="28" y={b.y} width={b.w} height="8" rx="2" fill="var(--border)" />
          </g>
        ))}
      </>
    )
  } else if (a.kind === 'dataset') {
    body = (
      <>
        {Array.from({ length: 6 }, (_, row) =>
          Array.from({ length: 4 }, (_, col) => (
            <rect key={`${row}${col}`} x={14 + col * 55} y={16 + row * 22} width={row === 0 ? 46 : 20 + r() * 26} height="10" rx="2" fill={row === 0 ? 'var(--brand)' : 'var(--border-strong)'} opacity={row === 0 ? 0.7 : 1} />
          )),
        )}
      </>
    )
  } else if (a.kind === 'log' || a.kind === 'receipt' || a.kind === 'report') {
    body = (
      <>
        {Array.from({ length: 9 }, (_, i) => (
          <g key={i}>
            <rect x="14" y={14 + i * 14} width="14" height="6" rx="2" fill="var(--text-faint)" opacity="0.5" />
            <rect x="34" y={14 + i * 14} width={30 + r() * 150} height="6" rx="2" fill={i === 8 && a.kind === 'log' ? 'var(--success)' : 'var(--border-strong)'} />
          </g>
        ))}
      </>
    )
  } else {
    const Icon = KIND_ICON[a.kind]
    body = (
      <foreignObject x="0" y="0" width={w} height={h}>
        <div className="flex size-full items-center justify-center text-text-faint">
          <Icon className="size-10" strokeWidth={1.25} />
        </div>
      </foreignObject>
    )
  }
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      role="img"
      aria-label={`Generated placeholder for ${a.name}`}
      className={cn('block w-full rounded-md border border-border bg-bg', large ? 'max-h-[60vh]' : 'h-[120px]')}
      preserveAspectRatio="xMidYMid slice"
    >
      {body}
    </svg>
  )
}

function parseCsv(text: string): string[][] {
  return text
    .trim()
    .split('\n')
    .map((l) => l.split(','))
}

function langOf(a: Artifact): string {
  if (/\.json$/.test(a.name) || a.kind === 'receipt') return 'json'
  if (/\.diff$|\.patch$/.test(a.name)) return 'diff'
  return 'shellscript'
}

/** Plain text with a wrap toggle (on by default) and "Copy all": a log is read, searched and pasted, not highlighted. */
function TextViewer({ text, label }: { text: string; label: string }) {
  const [wrap, setWrap] = useState(true)
  const [copied, setCopied] = useState(false)
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Button type="button" variant="outline" size="sm" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
          {wrap && <Check />}
          Wrap lines
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            }, () => {})
          }}
        >
          {copied ? <Check /> : <Copy />}
          Copy all
        </Button>
      </div>
      <pre tabIndex={0} aria-label={label} className={cn('max-h-[70vh] overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5', wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre')}>
        {text}
      </pre>
    </div>
  )
}

/** Only these kinds are documents; an `.html` name on a log or a dataset is shown as the text it is. */
const DOCUMENT_KINDS: Artifact['kind'][] = ['report', 'diagram', 'other']
const isHtmlDocument = (a: Artifact) => DOCUMENT_KINDS.includes(a.kind) && /\.html?$/.test(a.name)

/**
 * An HTML document runs in the same sandboxed frame as its widget, and only while agent HTML is on: the frame is
 * drawn from the live `agentHtml` value on every render, so turning the widgets addon off replaces it with source.
 * "View source" shows the bytes instead.
 */
function HtmlViewer({ a, agentHtml }: { a: Artifact; agentHtml: boolean }) {
  const [source, setSource] = useState(false)
  const node = frameNode.safeParse({ type: 'frame', title: `Sandboxed preview of ${a.name}`, html: frameDocument(a.preview!, {}), height: 520 })
  const framed = agentHtml && !source && node.success
  return (
    <div className={cn('space-y-2', agentHtml && 'rounded-lg border p-2', agentHtml && addonHairline)} data-addon={agentHtml ? 'widgets' : undefined}>
      <div className="flex items-center gap-2">
        {agentHtml && <AddonBadge name="widgets" />}
        {framed && <Pill tone="neutral">Sandboxed preview</Pill>}
        {agentHtml && node.success && (
          <Button type="button" variant="outline" size="sm" aria-pressed={source} onClick={() => setSource(!source)}>
            {source && <Check />}
            View source
          </Button>
        )}
        {!agentHtml && <span className="text-[12px] text-text-muted">Agent HTML is off in this workspace, so only the source is shown.</span>}
      </div>
      {framed ? <FrameNode node={node.data} fallback={<p className="text-[13px] text-text-muted">The preview left its sandbox and was removed.</p>} /> : <CodeBlock language="html" text={a.preview!} />}
    </div>
  )
}

/** The content of one artifact (also the Artifacts page's preview pane): same sandbox and agent-HTML rule everywhere. */
export function Viewer({ a, agentHtml }: { a: Artifact; agentHtml: boolean }) {
  if (a.kind === 'screenshot')
    return (
      <div className="space-y-2">
        <Thumb a={a} large />
        <p className="text-[12px] text-text-faint">Placeholder rendering. The real host serves the file; the hash below is what an approval binds.</p>
      </div>
    )
  if (!a.preview) return <p className="text-[13px] text-text-muted">No inline preview for this artifact.</p>
  if (isHtmlDocument(a)) return <HtmlViewer a={a} agentHtml={agentHtml} />
  if (a.kind === 'log') return <TextViewer text={a.preview} label={`Log ${a.name}`} />
  if (a.kind === 'dataset' || /\.csv$/.test(a.name)) {
    const rows = parseCsv(a.preview)
    return (
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            {rows[0].map((c) => (
              <TableHead key={c} className="font-mono text-[11px]">
                {c}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.slice(1).map((r, i) => (
            <TableRow key={i}>
              {r.map((c, j) => (
                <TableCell key={j} className="font-mono text-[12px]">
                  {c}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )
  }
  return <CodeBlock language={langOf(a)} text={a.preview} />
}

const isHttp = (u?: string) => !!u && /^https?:\/\//i.test(u)

function Meta({ a, jump }: { a: Artifact; jump: (j: Jump) => void }) {
  return (
    <div className="mt-2 space-y-1 text-[11px] text-text-muted">
      <div className="flex flex-wrap items-center gap-1.5">
        {a.ac && (
          <button type="button" onClick={() => jump({ tab: 'acceptance', id: `ac-${a.ac}` })} className="rounded border border-border px-1.5 font-mono hover:bg-surface-2">
            {a.ac}
          </button>
        )}
        {a.task && (
          <button type="button" onClick={() => jump({ tab: 'acceptance', id: `task-${a.task}` })} className="rounded border border-border px-1.5 font-mono hover:bg-surface-2">
            {a.task}
          </button>
        )}
        <Mono className="text-[11px] text-text-faint">{shortHash(a.sha256, 8)}</Mono>
        {a.bytes > 0 && <span className="text-text-faint">{fmtBytes(a.bytes)}</span>}
      </div>
    </div>
  )
}

export function Artifacts({ ticket, viewer, jump, focus }: TabProps & { focus?: string }) {
  const [open, setOpen] = useState<Artifact | null>(null)
  /** The card button that opened the drawer; closing returns focus to it. */
  const opener = useRef<HTMLElement | null>(null)
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const agentHtml = addonActive(workspaceOfTicket(ticket.key, workspaces.data ?? []), 'widgets')
  useEffect(() => {
    if (!focus) return
    const a = ticket.artifacts.find((x) => `artifact-${x.name}` === focus)
    if (a && !a.url && !a.addon) {
      opener.current = document.getElementById(focus)?.querySelector('button') ?? null
      setOpen(a)
    }
  }, [focus, ticket.artifacts])

  if (ticket.artifacts.length === 0)
    return <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">No artifacts yet. Agents attach evidence with orch artifact add.</p>

  return (
    <>
      <ul className="grid grid-cols-2 gap-3 xl:grid-cols-3" aria-label="Artifacts">
        {ticket.artifacts.map((a) => {
          const Icon = KIND_ICON[a.kind]
          const external = a.kind === 'link' && isHttp(a.url)
          const inner = (
            <>
              <div className="relative">
                {a.addon ? (
                  <div className={cn('flex h-[120px] w-full items-center justify-center rounded-md border', addonTile)}>
                    <AddonBadge name={a.addon} className="size-8 text-lg" />
                  </div>
                ) : (
                  <Thumb a={a} />
                )}
                {a.addon && <span className="absolute bottom-1.5 right-2 font-mono text-[10px] text-text-muted">{a.addon}</span>}
              </div>
              <div className="mt-2 flex items-center gap-1.5">
                <Icon className="size-3.5 shrink-0 text-text-muted" />
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{a.name}</span>
                {external && <ExternalLink className="size-3 text-text-faint" aria-hidden />}
              </div>
              {a.label && <p className="mt-0.5 line-clamp-2 text-[12px] text-text-muted">{a.label}</p>}
              <div className="mt-1.5 flex items-center gap-1.5">
                <Pill>{a.addon ? 'addon artifact' : a.kind}</Pill>
                <span className="truncate text-[11px] text-text-faint">
                  by {viewer.name(a.added_by)} · {ago(a.at)}
                </span>
              </div>
            </>
          )
          const frame = 'block w-full rounded-md text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50'
          return (
            <li
              key={a.name}
              id={`artifact-${a.name}`}
              data-kind={a.kind}
              className={cn('scroll-mt-4 rounded-lg border bg-surface p-2.5 transition-colors', a.addon ? addonHairline : 'border-border hover:border-border-strong')}
            >
              {external ? (
                <a href={a.url} target="_blank" rel="noopener noreferrer nofollow" className={frame}>
                  {inner}
                </a>
              ) : a.addon ? (
                <div>{inner}</div>
              ) : (
                <button type="button" className={cn(frame, 'cursor-pointer')} aria-label={`Open ${a.name}`} onClick={(e) => {
                    opener.current = e.currentTarget
                    setOpen(a)
                  }}>
                  {inner}
                </button>
              )}
              <Meta a={a} jump={jump} />
            </li>
          )
        })}
      </ul>

      <ArtifactDrawer artifact={open} agentHtml={agentHtml} onClose={() => setOpen(null)} opener={opener} />
    </>
  )
}

/**
 * The artifact drawer (ticket tab and the workspace Artifacts page). The viewer is keyed by name + sha256, HTML runs
 * only for document kinds and only while agent HTML is on (see HtmlViewer). Closing returns focus to `opener`.
 */
export function ArtifactDrawer({
  artifact: open,
  agentHtml,
  onClose,
  opener,
  context,
  loading = false,
}: {
  /** The content is still on its way: a skeleton instead of the viewer. */
  loading?: boolean
  artifact: Artifact | null
  agentHtml: boolean
  onClose: () => void
  opener: { current: HTMLElement | null }
  /** Extra line under the description (the Artifacts page names the ticket). */
  context?: ReactNode
}) {
  return (
    <Sheet open={!!open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        className="w-[640px] max-w-[92vw] gap-0 border-border bg-surface sm:max-w-[640px]"
        onCloseAutoFocus={(e) => {
          e.preventDefault()
          opener.current?.focus()
        }}
      >
        {open && (
          <>
            <SheetHeader className="border-b border-border">
              <SheetTitle className="break-all font-mono text-[14px]">{open.name}</SheetTitle>
              <SheetDescription>
                {open.kind} · {fmtBytes(open.bytes)} · sha256 {shortHash(open.sha256, 12)} · by {open.added_by === 'host' ? 'orch' : agentName(open.added_by)} · {ago(open.at)}
                {open.ac && ` · proves ${open.ac}`}
                {open.task && ` · from ${open.task}`}
              </SheetDescription>
              {context}
            </SheetHeader>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              {loading ? <Skeleton className="h-60 w-full" aria-label="Loading the artifact" /> : <Viewer key={open.name + open.sha256} a={open} agentHtml={agentHtml} />}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
