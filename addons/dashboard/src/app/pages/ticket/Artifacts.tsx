import { ArrowUpRight, BarChart3, Check, Copy, Eye, FileJson, Info, FileText, Image as ImageIcon, Link2, ScrollText, Table2, Workflow } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
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
import { highlight, langForFile, MAX_HIGHLIGHT_LINES, tokenClass, type Token } from '@/addon-ui/highlight'
import { Skeleton } from '@/components/ui/skeleton'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { agentName, ago, fmtBytes, Mono, Pill, shortHash, type Jump, type TabProps } from './shared'
import { addonHairline, addonTile } from '@/addon-ui/addonClasses'
import { queries } from '@/api/queries'
import { artifactTextQuery, sameDigest, capText, looksBinary, TEXT_PREVIEW_BYTES } from '@/api/artifactText'
import { prose, visible } from '@/components/sign/visible'
import { verifiedMedia } from '@/api/evidenceMedia'
import { SafeMarkdownView } from '@/addon-ui/SafeMarkdownView'
import { NoPreviewCard } from './EvidenceCard'
import { EvidenceThumb, TypeBadge } from './EvidenceThumb'

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

/** The lines of a text as tokens: coloured when the highlighter knows the language (and the text is small enough), else plain. */
function useTokenLines(text: string, lang: string | null | undefined, on: boolean): Token[][] {
  const plain = () => text.split('\n').map((l) => [{ content: l } as Token])
  const [lines, setLines] = useState<{ lang: string; text: string; lines: Token[][] } | null>(null)
  useEffect(() => {
    setLines(null)
    if (!on || !lang) return
    let live = true
    highlight(text, lang)
      .then((l) => live && l && setLines({ lang, text, lines: l }))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [text, lang, on])
  // The whole text is compared (no length/prefix key): a coloured view of another text is never shown.
  return on && lang && lines?.lang === lang && lines.text === text ? lines.lines : plain()
}

/**
 * The most the reformatted view may be (the file is agent-supplied: indenting nested arrays multiplies its size) and the
 * deepest nesting it is tried on. Over either, only the file's own text is offered.
 */
const REFORMAT_MAX_DEPTH = 64
const maxDepth = (t: string): number => {
  let d = 0
  let max = 0
  let inStr = false
  for (let i = 0; i < t.length; i++) {
    const c = t[i]
    if (inStr) {
      if (c === '\\') i++
      else if (c === '"') inStr = false
    } else if (c === '"') inStr = true
    else if (c === '[' || c === '{') max = Math.max(max, ++d)
    else if (c === ']' || c === '}') d--
  }
  return max
}
/**
 * Lays JSON out for reading without parsing it into values: every token (strings, numbers, keys, even duplicate keys or
 * 1e400) stays byte for byte as the file has it; only the white space between tokens changes.
 */
function indentJson(text: string): string {
  let out = ''
  let depth = 0
  let inStr = false
  const nl = () => '\n' + '  '.repeat(depth)
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inStr) {
      out += c
      if (c === '\\') out += text[++i] ?? ''
      else if (c === '"') inStr = false
    } else if (c === '"') {
      inStr = true
      out += c
    } else if (c === ' ' || c === '\n' || c === '\t' || c === '\r') {
      continue
    } else if (c === '{' || c === '[') {
      let j = i + 1
      while (j < text.length && /\s/.test(text[j])) j++
      if (text[j] === (c === '{' ? '}' : ']')) {
        out += c + text[j]
        i = j
      } else {
        depth++
        out += c + nl()
      }
    } else if (c === '}' || c === ']') {
      depth--
      out += nl() + c
    } else if (c === ',') out += c + nl()
    else if (c === ':') out += ': '
    else out += c
  }
  return out
}
export function reformatJson(text: string): string | undefined {
  if (!text.split('\n').some((l) => l.length > MINIFIED_LINE) || maxDepth(text) > REFORMAT_MAX_DEPTH) return undefined
  try {
    JSON.parse(text) // only to know it is JSON; the output never comes from the parsed values
  } catch {
    return undefined // not valid JSON: only the text it is
  }
  const out = indentJson(text)
  return out.length > TEXT_PREVIEW_BYTES || out.split('\n').length > MAX_HIGHLIGHT_LINES ? undefined : prose(out)
}

function JsonText({ shown, safe, name }: { shown: string; safe: string; name: string }) {
  const reformatted = useMemo(() => reformatJson(shown), [shown])
  return <TextViewer text={safe} label={`JSON of ${name}`} lang="json" reformatted={reformatted} />
}

/** A line is "minified" when one line is this long: only then is a reformatted view offered. */
const MINIFIED_LINE = 200

/**
 * A text file: line numbers, colours by file type (tokens as text nodes with classes: nothing is parsed as markup), a
 * Formatted / Raw switch, Wrap lines (on) and Copy all. `text` is already made visible (`prose`). `rendered` is a
 * formatted view of the same text (markdown), `reformatted` another layout of it (minified JSON); Raw is always the file's own text.
 */
function TextViewer({ text, label, lang, rendered, reformatted, rawLabel = 'Raw' }: { text: string; label: string; lang?: string | null; rendered?: ReactNode; reformatted?: string; rawLabel?: string }) {
  const [wrap, setWrap] = useState(true)
  const hasFormatted = !!lang || !!rendered || !!reformatted
  const [formatted, setFormatted] = useState(true)
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
  const view = formatted && hasFormatted
  const shownText = view && reformatted ? reformatted : text
  const lines = useTokenLines(shownText, lang, view)
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {hasFormatted && (
          <div role="group" aria-label="View" className="inline-flex overflow-hidden rounded-md border border-border text-[12px]">
            {([true, false] as const).map((f) => (
              <button key={String(f)} type="button" aria-pressed={formatted === f} onClick={() => setFormatted(f)} className={cn('px-2.5 py-1', formatted === f ? 'bg-brand-soft text-brand' : 'text-text-muted hover:bg-surface-2')}>
                {f ? 'Formatted' : rawLabel}
              </button>
            ))}
          </div>
        )}
        {!(view && rendered) && (
          <Button type="button" variant="outline" size="sm" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
            {wrap && <Check />}
            Wrap lines
          </Button>
        )}
        <Button type="button" variant="outline" size="sm" onClick={copyAll}>
          {copy === 'copied' ? <Check /> : <Copy />}
          Copy all
        </Button>
        <span role="status" className="text-[12px] text-text-muted">
          {copy === 'copied' ? 'Copied' : copy === 'failed' ? 'Could not copy. Select the text and copy it instead.' : ''}
        </span>
      </div>
      {view && reformatted && <p className="text-[12px] text-text-muted">Reformatted view for reading: {rawLabel} shows the file as it is.</p>}
      {view && rendered ? (
        rendered
      ) : (
        <pre tabIndex={0} aria-label={label} className="max-h-[70vh] overflow-auto rounded-md border border-border bg-bg py-2 font-mono text-[12px] leading-5">
          {lines.slice(0, MAX_HIGHLIGHT_LINES).map((line, i) => (
            <div key={i} data-ln={view ? i + 1 : undefined} className={cn('min-h-5 pr-3', view ? 'pl-12 before:float-left before:-ml-10 before:w-8 before:select-none before:text-right before:text-text-faint before:content-[attr(data-ln)]' : 'pl-3', wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre')}>
              {line.map((t, j) => (
                <span key={j} className={tokenClass(t.color)}>
                  {t.content}
                </span>
              ))}
            </div>
          ))}
        </pre>
      )}
      {!(view && rendered) && lines.length > MAX_HIGHLIGHT_LINES && <p className="text-[12px] text-text-muted">Showing the first {MAX_HIGHLIGHT_LINES} lines. Copy all copies the whole text.</p>}
    </div>
  )
}

/** Only these kinds are documents; an `.html` name on a log or a dataset is shown as the text it is. */
const DOCUMENT_KINDS: Artifact['kind'][] = ['report', 'diagram', 'other']
const isHtmlDocument = (a: Artifact) => DOCUMENT_KINDS.includes(a.kind) && /\.html?$/.test(a.name)

/**
 * An HTML document of a ticket (evidence) is drawn in the inert sandboxed frame always (`evidence`, set by Viewer only).
 * Without `evidence` it would follow the live `agentHtml` value, so turning the widgets addon off replaces it with source.
 * "View source" shows the bytes instead, and then reads "Show preview".
 */
function HtmlViewer({ a, agentHtml: agentHtmlOn, evidence = false }: { a: Artifact; agentHtml: boolean; /** An HTML artifact of the ticket (evidence): always the inert frame, whatever the widgets switch says. Only RemotePreview (a file read from the host and digest-checked) sets it; inline page content keeps the add-on gate. */ evidence?: boolean }) {
  // Evidence HTML needs no add-on: the frame is inert (sandbox="", sanitized, no network), the same in both cases.
  const agentHtml = agentHtmlOn || evidence
  // A page that relied on scripts opens on its source with core's notice (round 2 #6); its static preview stays one click away.
  const scripted = usesScripts(a.preview!)
  const [source, setSource] = useState(scripted)
  // Agent HTML is drawn inert (no scripts, sanitized: security review #1), at a fixed height the person can drag.
  const node = frameNode.safeParse({ type: 'frame', title: `Sandboxed preview of ${visible(a.name)}`, html: a.preview!, height: 520 })
  const framed = agentHtml && !source && node.success
  return (
    <div className={cn('space-y-2', agentHtml && 'rounded-lg border p-2', agentHtml && (evidence ? 'border-border' : addonHairline))} data-addon={agentHtml && !evidence ? 'widgets' : undefined}>
      <div className="flex flex-wrap items-center gap-2">
        {agentHtml && !evidence && <AddonBadge name="widgets" />}
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

const NO_PREVIEW = <p className="text-[13px] text-text-muted">No inline preview for this kind of file. Its content is not shown here; the sha256 above is what an approval binds.</p>

/** A small CSV as a table (the first 200 rows and 40 columns); a bigger one stays text. */
function CsvTable({ text }: { text: string }) {
  const rows = parseCsv(text)
  const shown = rows.slice(0, 200).map((r) => r.slice(0, 40))
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {shown[0].map((c, i) => (
            <TableHead key={i} className="font-mono text-[11px]">
              {c}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {shown.slice(1).map((r, i) => (
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

/** A diff, coloured by line (added, removed, hunk header). Plain text lines: nothing is parsed as markup. */
function DiffView({ text, label }: { text: string; label: string }) {
  return (
    <pre tabIndex={0} aria-label={label} className="max-h-[70vh] overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5">
      {text.split('\n').map((l, i) => (
        <div key={i} data-diff={l.startsWith('+++') || l.startsWith('---') ? 'file' : l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : l.startsWith('@@') ? 'hunk' : undefined} className={cn('whitespace-pre-wrap break-words', l.startsWith('+') && !l.startsWith('+++') && 'bg-success-soft text-success', l.startsWith('-') && !l.startsWith('---') && 'bg-danger-soft text-danger', l.startsWith('@@') && 'text-info')}>
          {l || ' '}
        </div>
      ))}
    </pre>
  )
}

/** A picture that was not asked for (an SVG or GIF in an auto-opened list): not decoded until the person clicks. */
function HeldPicture({ a, url, kind }: { a: Artifact; url: string; kind: 'svg' | 'gif' }) {
  const [shown, setShown] = useState(false)
  if (shown) return <PictureView a={a} url={url} />
  return (
    <Button type="button" variant="outline" size="sm" onClick={() => setShown(true)}>
      Show {kind === 'svg' ? 'SVG' : 'animated GIF'} picture
    </Button>
  )
}

/** A picture from digest-checked bytes, shown only as an <img>: scaled to fit, one click to the real size. */
function PictureView({ a, url }: { a: Artifact; url: string }) {
  const [big, setBig] = useState(false)
  return (
    <div className="space-y-1.5">
      <div className={cn('rounded-md border border-border bg-bg p-1', big ? 'max-h-[75vh] overflow-auto' : 'flex justify-center')}>
        <button type="button" aria-label={big ? `Show ${visible(a.name)} scaled to fit` : `Enlarge ${visible(a.name)}`} aria-pressed={big} onClick={() => setBig(!big)} className={cn('block outline-none focus-visible:ring-2 focus-visible:ring-ring', big ? 'cursor-zoom-out' : 'cursor-zoom-in')}>
          {/* The one place a file's bytes become a picture: a data: URL in an <img> (no script, no network, even for SVG). */}
          <img src={url} alt={`Picture ${visible(a.name)}`} referrerPolicy="no-referrer" className={big ? 'max-h-[2000px] max-w-[2000px] object-contain' : 'max-h-80 max-w-full object-contain'} />
        </button>
      </div>
      <p className="text-[12px] text-text-muted">{big ? 'Full size. Click the picture to scale it to fit.' : 'Scaled to fit. Click the picture to enlarge.'}</p>
    </div>
  )
}

/** Big sizes in MB (the shared formatter stops at KB). */
const mb = (n: number) => (n >= 1024 * 1024 ? `${(n / (1024 * 1024)).toFixed(1)} MB` : fmtBytes(n))
const extOf = (name: string) => /\.([A-Za-z0-9]+)$/.exec(name)?.[1].toLowerCase() ?? ''

/**
 * The ladder for an evidence FILE, read from the host through the dashboard server (reads.py) and previewed only from
 * bytes whose sha256 is the ticket's (the server checks it, and the picture path checks it again here):
 *   1 HTML    the inert sandboxed frame (no scripts, no network)           4 PDF   no preview: its viewer runs in the page
 *   2 picture <img> from a data: URL, SVG included, never markup           5 else  a card that says why (never a blank panel)
 *   3 text    plain text (hidden characters escaped); json, csv, md, diff get their own view
 * A digest that differs from the ticket's, a file read only in part, or a binary file gets the card.
 */
function RemotePreview({ a, ticket, unavailable, holdPictures = false }: { a: Artifact; ticket: string; /** Shown when the host cannot read the file at all (a demo has no bytes). */ unavailable?: ReactNode; /** The file opened by itself (nobody clicked): an SVG or GIF, by the TYPE the server read from its bytes, waits for a click. */ holdPictures?: boolean }) {
  const workspaces = useQuery(queries.workspaces())
  const home = workspaceOfTicket(ticket, workspaces.data ?? [])
  const q = useQuery({ ...artifactTextQuery(home?.id ?? '', ticket, a.name, a.sha256), enabled: !!home })
  if (!home && !workspaces.isPending) return unavailable ?? <NoPreviewCard a={a} why="This ticket's workspace is not available, so the file cannot be read." />
  if (q.isPending) return <Skeleton className="h-40 w-full" aria-label="Loading the file" />
  if (q.isError || !q.data) return unavailable ?? <NoPreviewCard a={a} why="The host could not read this file, so there is no preview." />
  const d = q.data
  // Only the file the ticket lists: the digest the host read must be the one on the ticket.
  if (!sameDigest(d.digest, a.sha256)) return <NoPreviewCard a={a} warn why="This file does not match the digest the ticket records, so it is not shown. Do not rely on it as evidence." />
  if (d.truncated || !d.verified) return <NoPreviewCard a={a} why={`Too large to preview: ${mb(Math.max(d.size, a.bytes ?? 0))}, the limit is ${mb(5.25 * 1024 * 1024)}.`} />
  const offered = d.media ?? d.image
  if (offered) {
    const url = verifiedMedia(offered, a.sha256)
    return url ? holdPictures && (offered.type === 'svg' || offered.type === 'gif') ? <HeldPicture a={a} url={url} kind={offered.type} /> : <PictureView a={a} url={url} /> : <NoPreviewCard a={a} warn why="The picture does not match the digest the ticket records, so it is not shown." />
  }
  const ext = extOf(a.name)
  if (d.binary || d.text == null || looksBinary(d.text)) {
    return <NoPreviewCard a={a} why={ext === 'pdf' ? 'PDF documents are not previewed here: a PDF viewer would run inside this page. Open the file from the ticket on the host.' : `Binary file${ext ? ` (type .${ext})` : ''}: there is no text to show.`} />
  }
  const { shown, cut, bytes } = capText(d.text.replace(/\r\n/g, '\n'))
  // HTML evidence goes through the same viewer as an inline page (inert sandboxed frame). A page cut at 64 KiB
  // would be a different document from the one the digest names: not previewed.
  if (/^html?$/.test(ext))
    return cut ? <NoPreviewCard a={a} why={`Too large to preview as a page: ${fmtBytes(bytes)}, the limit is 64 KiB.`} /> : <HtmlViewer a={{ ...a, preview: shown }} agentHtml={false} evidence />
  const note = (
    <p className="flex flex-wrap items-center gap-x-1 text-[12px] text-text-muted">
      {cut ? `Showing the first ${TEXT_PREVIEW_BYTES / 1024} KiB of ${fmtBytes(Math.max(bytes, d.size))}. ` : ''}
      Shown as text. Nothing in the file is run.
      <span role="img" aria-label={'Hidden characters are shown as \\u{…}'} title={'Hidden characters are shown as \\u{…}'} className="inline-flex cursor-help"><Info className="size-3.5" aria-hidden /></span>
    </p>
  )
  const safe = prose(shown)
  const lang = langForFile(a.name)
  if (ext === 'json' && !cut) {
    // The file's own text, coloured. Never re-parsed (that would hide duplicate keys and rounded numbers). Only a minified
    // file (one very long line) is also offered reformatted, and says so.
    return (
      <div className="space-y-2">
        {note}
        <JsonText shown={shown} safe={safe} name={a.name} />
      </div>
    )
  }
  if (ext === 'csv' && !cut && parseCsv(shown).length > 1 && parseCsv(shown).length <= 200) return <CsvTable text={safe} />
  if (/^(md|markdown)$/.test(ext) && !cut)
    return (
      <div className="space-y-2">
        {note}
        <TextViewer
          text={safe}
          label={`Text of ${a.name}`}
          lang="markdown"
          rawLabel="Source"
          rendered={
            <div className="space-y-2">
              <p className="text-[12px] text-text-muted">Notes shown with basic formatting. Links are shown as text with their address and cannot be clicked; pictures are not loaded.</p>
              <div className="rounded-md border border-border bg-bg p-3 text-[13px]">
                <SafeMarkdownView text={safe} inertLinks />
              </div>
            </div>
          }
        />
      </div>
    )
  if (/^(diff|patch)$/.test(ext)) return <div className="space-y-2">{note}<DiffView text={safe} label={`Diff ${a.name}`} /></div>
  return (
    <div className="space-y-2">
      {note}
      <TextViewer text={safe} label={`Text of ${a.name}`} lang={lang} />
    </div>
  )
}

/** The content of one artifact (also the Artifacts page's preview pane): same sandbox and agent-HTML rule everywhere. */
export function Viewer({ a, agentHtml, ticket, holdPictures = false }: { a: Artifact; agentHtml: boolean; /** The ticket it is on: lets a file with no inline content be read as text. */ ticket?: string; /** Opened without a click (an evidence list): SVG and GIF wait for one. */ holdPictures?: boolean }) {
  const hasFile = a.bytes != null && !a.addon && !a.url
  if (a.kind === 'screenshot' && ticket && hasFile && !a.preview)
    return (
      <RemotePreview
        a={a}
        ticket={ticket}
        holdPictures={holdPictures}
        unavailable={
          <div className="space-y-2">
            <Thumb a={a} large />
            <p className="text-[12px] text-text-faint">Placeholder rendering: the host could not serve this file. The sha256 above is what an approval binds.</p>
          </div>
        }
      />
    )
  if (a.kind === 'screenshot')
    return (
      <div className="space-y-2">
        <Thumb a={a} large />
        <p className="text-[12px] text-text-faint">Placeholder rendering. The real host serves the file; the sha256 above is what an approval binds.</p>
      </div>
    )
  if (!a.preview) return ticket && hasFile ? <RemotePreview a={a} ticket={ticket} holdPictures={holdPictures} /> : NO_PREVIEW
  if (isHtmlDocument(a)) return <HtmlViewer a={a} agentHtml={agentHtml} />
  if (a.kind === 'log') return <TextViewer text={a.preview} label={`Log ${a.name}`} />
  if (a.kind === 'dataset' || /\.csv$/.test(a.name)) {
    return <CsvTable text={a.preview} />
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
      {!!a.bytes && <span>{fmtBytes(a.bytes)}</span>}
    </div>
  )
}

export function Artifacts({ ticket, viewer, jump, focus }: TabProps & { focus?: string }) {
  const [open, setOpen] = useState<Artifact | null>(null)
  /** The drawer opened by a jump (a link), not by a click on Preview: an SVG or GIF waits for a click. */
  const [byJump, setByJump] = useState(false)
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
      setByJump(true)
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
                <EvidenceThumb a={a} ticket={ticket.key} />
                <div className="mt-2 flex items-center gap-1.5">
                  <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium" title={a.name}>
                    {a.name}
                  </span>
                  <TypeBadge a={a} />
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
                      setByJump(false)
                      setOpen(a)
                    }}
                  />
                </div>
              </li>
            )
          })}
        </ul>
      </div>

      <ArtifactDrawer artifact={open} ticket={ticket.key} agentHtml={agentHtml} holdPictures={byJump} onClose={() => setOpen(null)} opener={opener} />
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
  ticket,
  holdPictures = false,
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
  ticket?: string
  /** Opened by a link, not a click: an SVG or GIF waits for a click. */
  holdPictures?: boolean
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
                    {!!open.bytes && <span>{fmtBytes(open.bytes)}</span>}
                    {context}
                  </SheetDescription>
                  <ArtifactFacts a={open} by={by} />
                </>
              )}
            </SheetHeader>
            <div className="min-h-0 flex-1 overflow-auto p-4">{body ?? <Viewer key={open.name + open.sha256} a={open} agentHtml={agentHtml} ticket={ticket} holdPictures={holdPictures} />}</div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
