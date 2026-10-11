import { ArrowUpRight, BarChart3, Check, Copy, Eye, FileJson, FileText, Image as ImageIcon, Link2, ScrollText, Table2, Workflow } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { addonActive } from '@/api/addons'
import { workspaceOfTicket } from '@/api/workspaces'
import { FrameNode } from '@/addon-ui/FrameNode'
import { usesScripts } from '@/addon-ui/frameSanitize'
import { ScriptedPreview } from '@/addon-ui/ScriptedPreview'
import { frameNode } from '@/addon-ui/nodes'
import { Button, buttonVariants } from '@/components/ui/button'
import type { Artifact } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { useSlot } from '@/addon-ui/slots'
import { CodeBlock } from '@/addon-ui/CodeBlock'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { agentName, ago, fmtBytes, Mono, Pill, shortHash, type Jump, type TabProps } from './shared'
import { addonHairline, addonTile } from '@/addon-ui/addonClasses'
import { queries } from '@/api/queries'

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
  const [copy, setCopy] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const show = (s: 'copied' | 'failed') => {
    setCopy(s)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopy('idle'), s === 'copied' ? 1500 : 4000)
  }
  const copyAll = () => {
    // No clipboard (an insecure context, an old browser) or a refused write: say so instead of looking done.
    if (!navigator.clipboard?.writeText) return show('failed')
    navigator.clipboard.writeText(text).then(() => show('copied'), () => show('failed'))
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
          {wrap && <Check />}
          Wrap lines
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={copyAll}>
          {copy === 'copied' ? <Check /> : <Copy />}
          Copy all
        </Button>
        <span role="status" className="text-[12px] text-text-muted">
          {copy === 'copied' ? 'Copied' : copy === 'failed' ? 'Could not copy. Select the text and copy it instead.' : ''}
        </span>
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
 * "View source" shows the bytes instead, and then reads "Show preview".
 */
function HtmlViewer({ a, agentHtml }: { a: Artifact; agentHtml: boolean }) {
  // A page that relied on scripts opens on its source with core's notice (round 2 #6); its static preview stays one click away.
  const scripted = usesScripts(a.preview!)
  const [source, setSource] = useState(scripted)
  // Agent HTML is drawn inert (no scripts, sanitized: security review #1), at a fixed height the person can drag.
  const node = frameNode.safeParse({ type: 'frame', title: `Sandboxed preview of ${a.name}`, html: a.preview!, height: 520 })
  const framed = agentHtml && !source && node.success
  return (
    <div className={cn('space-y-2', agentHtml && 'rounded-lg border p-2', agentHtml && addonHairline)} data-addon={agentHtml ? 'widgets' : undefined}>
      <div className="flex items-center gap-2">
        {agentHtml && <AddonBadge name="widgets" />}
        {framed && <Pill tone="neutral">Sandboxed preview</Pill>}
        {agentHtml && node.success && (
          <Button type="button" variant="outline" size="sm" onClick={() => setSource(!source)}>
            {source ? 'Show preview' : 'View source'}
          </Button>
        )}
        {!agentHtml && <span className="text-[12px] text-text-muted">Agent HTML is off in this workspace, so only the source is shown.</span>}
      </div>
      {framed ? (
        <FrameNode node={node.data} fallback={<p className="text-[13px] text-text-muted">The preview left its sandbox and was removed.</p>} />
      ) : agentHtml && scripted && node.success ? (
        <ScriptedPreview html={a.preview!} onPreview={() => setSource(false)} />
      ) : (
        <CodeBlock language="html" text={a.preview!} />
      )}
    </div>
  )
}

/** The content of one artifact (also the Artifacts page's preview pane): same sandbox and agent-HTML rule everywhere. */
export function Viewer({ a, agentHtml }: { a: Artifact; agentHtml: boolean }) {
  if (a.kind === 'screenshot')
    return (
      <div className="space-y-2">
        <Thumb a={a} large />
        <p className="text-[12px] text-text-faint">Placeholder rendering. The real host serves the file; the sha256 above is what an approval binds.</p>
      </div>
    )
  if (!a.preview) return <p className="text-[13px] text-text-muted">No inline preview for this kind of file. Its content is not shown here; the sha256 above is what an approval binds.</p>
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
            <TableRow key={i} className="hover:bg-transparent">
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
/** How an artifact opens: a web link in a new tab, an addon's artifact in its addon, the rest in the viewer (Preview). */
export const openMode = (a: Pick<Artifact, 'kind' | 'url' | 'addon'>): 'external' | 'addon' | 'preview' => (a.kind === 'link' && isHttp(a.url) ? 'external' : a.addon ? 'addon' : 'preview')

/** A link that looks like one at rest (colour and underline), not only on hover: ticket keys, AC and task jumps. */
export const LINK = 'rounded-sm text-brand underline decoration-brand/40 underline-offset-2 outline-none hover:decoration-brand focus-visible:ring-2 focus-visible:ring-ring'

/** The addons' own pages in this workspace, by addon: an addon artifact links to its addon only when it has a page. */
export function useAddonPages() {
  const nav = useSlot('nav')
  return (addon: string) => nav.find((n) => n.addon === addon)
}
export type AddonPages = ReturnType<typeof useAddonPages>

/**
 * An honest tile for the grid and the ticket's cards: the type's icon and name. No generated "content" (it would
 * suggest evidence that is not there); real thumbnails wait for a host capability (DECISIONS-LOG, G3).
 */
export function TypeTile({ a }: { a: Pick<Artifact, 'kind' | 'addon'> }) {
  if (a.addon)
    return (
      <div aria-hidden className={cn('flex h-20 flex-col items-center justify-center gap-1.5 rounded-md border', addonTile)}>
        <AddonBadge name={a.addon} className="size-6 text-[13px]" />
        <span className="text-[11px] text-text-muted">{a.addon} addon</span>
      </div>
    )
  const Icon = KIND_ICON[a.kind]
  return (
    <div aria-hidden className="flex h-20 flex-col items-center justify-center gap-1.5 rounded-md border border-border bg-bg text-text-faint">
      <Icon className="size-6" strokeWidth={1.5} />
      <span className="text-[11px]">{a.kind === 'link' ? 'web link' : a.kind}</span>
    </div>
  )
}

const ACTION = 'h-7 gap-1.5 px-2 text-[12px]'

/**
 * The one action of an artifact, always visible (list row end, card foot): "Preview" opens the viewer; a web link
 * says it opens a new tab; an addon's artifact links to the addon's page, or says where it is shown. Names, labels and
 * card bodies are plain text: nothing else on the item is a target except the ticket link (DECISIONS-LOG, G3).
 */
export function ArtifactAction({ a, onPreview, current = false, addonPage }: { a: Pick<Artifact, 'name' | 'kind' | 'url' | 'addon'>; onPreview: (el: HTMLElement) => void; current?: boolean; addonPage: AddonPages }) {
  const mode = openMode(a)
  if (mode === 'external')
    return (
      <a href={a.url} target="_blank" rel="noopener noreferrer nofollow" aria-label={`Open link ${a.name} (opens in a new tab)`} className={cn(buttonVariants({ variant: 'outline', size: 'sm' }), ACTION)}>
        Open link
        <ArrowUpRight aria-hidden />
      </a>
    )
  if (mode === 'addon') {
    const page = addonPage(a.addon!)
    // Core words on the button; the addon's own title only as plain text beside it, with its id (no addon text as a verb).
    const who = `${page?.addonTitle ?? a.addon} (${a.addon})`
    return page ? (
      <span className="flex min-w-0 flex-col items-start gap-1">
        <Link to="/addon/$name/$page" params={{ name: page.addon, page: page.id }} aria-label={`Open addon page: ${who}`} className={cn(buttonVariants({ variant: 'outline', size: 'sm' }), ACTION)}>
          Open addon page
        </Link>
        <span className="flex w-full min-w-0 items-center gap-1 text-[11px] text-text-muted">
          <AddonBadge name={a.addon!} title={page.addonTitle} />
          <span className="min-w-0 truncate" title={who}>
            {who}
          </span>
        </span>
      </span>
    ) : (
      <span className="text-[12px] text-text-muted">Shown in the {a.addon} addon</span>
    )
  }
  return (
    <Button type="button" variant="outline" size="sm" data-preview className={cn(ACTION, current && 'border-brand/60 bg-brand-soft text-brand hover:bg-brand-soft hover:text-brand')} aria-label={`Preview ${a.name}`} onClick={(e) => onPreview(e.currentTarget)}>
      <Eye aria-hidden />
      Preview
    </Button>
  )
}

function Meta({ a, jump }: { a: Artifact; jump: (j: Jump) => void }) {
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-text-faint">
      {a.ac && (
        <button type="button" onClick={() => jump({ tab: 'acceptance', id: `ac-${a.ac}` })} className={cn(LINK, 'font-mono')}>
          Proves {a.ac}
        </button>
      )}
      {a.task && (
        <button type="button" onClick={() => jump({ tab: 'acceptance', id: `task-${a.task}` })} className={cn(LINK, 'font-mono')}>
          From {a.task}
        </button>
      )}
      <Mono className="text-[11px] text-text-faint">{shortHash(a.sha256, 8)}</Mono>
      {a.bytes > 0 && <span>{fmtBytes(a.bytes)}</span>}
    </div>
  )
}

export function Artifacts({ ticket, viewer, jump, focus }: TabProps & { focus?: string }) {
  const [open, setOpen] = useState<Artifact | null>(null)
  /** The Preview button that opened the drawer; closing returns focus to it. */
  const opener = useRef<HTMLElement | null>(null)
  const workspaces = useQuery(queries.workspaces())
  const agentHtml = addonActive(workspaceOfTicket(ticket.key, workspaces.data ?? []), 'widgets')
  const addonPage = useAddonPages()
  useEffect(() => {
    if (!focus) return
    const a = ticket.artifacts.find((x) => `artifact-${x.name}` === focus)
    if (a && openMode(a) === 'preview') {
      opener.current = document.getElementById(focus)?.querySelector<HTMLElement>('[data-preview]') ?? null
      setOpen(a)
    }
  }, [focus, ticket.artifacts])

  if (ticket.artifacts.length === 0)
    return <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">No artifacts yet. Agents attach evidence with orch artifact add.</p>

  return (
    <>
      {/* Columns follow the tab's own width (beside a wide dock there are fewer), as on the Artifacts page. */}
      <div className="@container/cards">
        <ul className="grid grid-cols-1 gap-3 @[28rem]/cards:grid-cols-2 @[44rem]/cards:grid-cols-3" aria-label="Artifacts">
          {ticket.artifacts.map((a) => {
            const Icon = KIND_ICON[a.kind]
            return (
              <li
                key={a.name}
                id={`artifact-${a.name}`}
                data-kind={a.kind}
                aria-current={open?.name === a.name ? 'true' : undefined}
                className={cn('flex scroll-mt-4 flex-col rounded-lg border bg-surface p-2.5', a.addon ? addonHairline : 'border-border')}
              >
                <TypeTile a={a} />
                <div className="mt-2 flex items-center gap-1.5">
                  <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium" title={a.name}>
                    {a.name}
                  </span>
                </div>
                {a.label && <p className="mt-0.5 line-clamp-2 text-[12px] text-text-muted">{a.label}</p>}
                <p className="mt-1 truncate text-[11px] text-text-faint">
                  by {viewer.name(a.added_by)} · {ago(a.at)}
                </p>
                <Meta a={a} jump={jump} />
                <div className="mt-auto pt-2">
                  <ArtifactAction
                    a={a}
                    addonPage={addonPage}
                    current={open?.name === a.name}
                    onPreview={(el) => {
                      opener.current = el
                      setOpen(a)
                    }}
                  />
                </div>
              </li>
            )
          })}
        </ul>
      </div>

      <ArtifactDrawer artifact={open} agentHtml={agentHtml} onClose={() => setOpen(null)} opener={opener} />
    </>
  )
}

/**
 * What the viewer shows above the content: kind and size, then (Artifacts page) the ticket it is on, prominent; who
 * added it, when, its hash and what it proves on one quiet line.
 */
export function ArtifactFacts({ a, by }: { a: Artifact; /** "Claude Code for Severin" where the page knows more than the ticket's actor label. */ by?: string }) {
  return (
    <p className="text-[11px] text-text-faint">
      Added by {by ?? (a.added_by === 'host' ? 'orch' : agentName(a.added_by))} · {ago(a.at)} · sha256 {shortHash(a.sha256, 12)}
      {a.ac && ` · proves ${a.ac}`}
      {a.task && ` · from ${a.task}`}
    </p>
  )
}

/**
 * The artifact drawer (ticket tab and the workspace Artifacts page). The viewer is keyed by name + sha256, HTML runs
 * only for document kinds and only while agent HTML is on (see HtmlViewer). Closing returns focus to `opener`.
 * `body` replaces the viewer (the Artifacts page's loading, missing and failed states); `denied` shows no facts at all.
 */
export function ArtifactDrawer({
  artifact: open,
  agentHtml,
  onClose,
  opener,
  context,
  by,
  body,
  denied = false,
}: {
  artifact: Artifact | null
  agentHtml: boolean
  onClose: () => void
  opener: { current: HTMLElement | null }
  /** The ticket it is on (the Artifacts page). */
  context?: ReactNode
  by?: string
  body?: ReactNode
  denied?: boolean
}) {
  return (
    <Sheet open={!!open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side="right"
        data-artifact-drawer
        className="w-[640px] max-w-[92vw] gap-0 border-border bg-surface sm:max-w-[640px]"
        onCloseAutoFocus={(e) => {
          e.preventDefault()
          opener.current?.focus()
        }}
      >
        {open && (
          <>
            <SheetHeader className="gap-1 border-b border-border pr-10">
              <SheetTitle className="break-all font-mono text-[14px]">{denied ? 'Artifact not available' : open.name}</SheetTitle>
              {denied ? (
                <SheetDescription>You can no longer see this artifact.</SheetDescription>
              ) : (
                <>
                  <SheetDescription className="flex flex-wrap items-center gap-x-1.5 text-[12px]">
                    <Pill>{open.kind}</Pill>
                    {open.bytes > 0 && <span>{fmtBytes(open.bytes)}</span>}
                    {context}
                  </SheetDescription>
                  <ArtifactFacts a={open} by={by} />
                </>
              )}
            </SheetHeader>
            <div className="min-h-0 flex-1 overflow-auto p-4">{body ?? <Viewer key={open.name + open.sha256} a={open} agentHtml={agentHtml} />}</div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
