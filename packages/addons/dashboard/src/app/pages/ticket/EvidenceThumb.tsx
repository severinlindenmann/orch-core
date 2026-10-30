// The thumbnail and type badge of an evidence card (run 8, 7). A thumbnail is never a live page: it is the first 8 lines of
// a text file as TEXT NODES (coloured by the same token classes as the preview), or the digest-verified picture (PNG, JPEG
// and WebP only), or the type icon. SVG, GIF, HTML, PDF, binary, oversized and digest-mismatched files get the icon.
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { artifactTextQuery, looksBinary, sameDigest } from '@/api/artifactText'
import { isRealHost } from '@/api/features'
import { verifiedMedia } from '@/api/evidenceMedia'
import { queries } from '@/api/queries'
import { workspaceOfTicket } from '@/api/workspaces'
import type { Artifact, TaskStatus } from '@/api/types'
import { highlight, langForFile, tokenClass, type Token } from '@/addon-ui/highlight'
import { prose, visible } from '@/components/sign/visible'
import { cn } from '@/lib/utils'
import { AUTO_OPEN_BYTES } from './EvidenceView'
import { TypeTile } from './Artifacts'

const THUMB_LINES = 8
const MAX_IN_FLIGHT = 4

/** At most this many thumbnail reads at once; the rest wait their turn. */
let inFlight = 0
const waiting: (() => void)[] = []
async function limited<T>(run: () => Promise<T>): Promise<T> {
  if (inFlight >= MAX_IN_FLIGHT) await new Promise<void>((r) => waiting.push(r))
  inFlight++
  try {
    return await run()
  } finally {
    inFlight--
    waiting.shift()?.()
  }
}

const ext = (name: string) => /\.([A-Za-z0-9]+)$/.exec(name)?.[1].toLowerCase() ?? ''

/** The result of a check, from what the HOST recorded only: a receipt artifact the host added (`added_by_kind` host) for a task
 *  whose receipt has an exit status. The file name and anything an agent wrote never count. null: not a recorded check. */
export type CheckResult = 'passed' | 'failed'
export function checkOf(a: Pick<Artifact, 'kind' | 'task' | 'added_by' | 'added_by_kind'>, tasks: ReadonlyArray<Pick<TaskStatus, 'id' | 'receipt'>> | undefined): CheckResult | null {
  if (a.kind !== 'receipt' || !(a.added_by_kind === 'host' || a.added_by === 'host') || !a.task) return null
  const exit = tasks?.find((t) => t.id === a.task)?.receipt?.exit
  return typeof exit !== 'number' ? null : exit === 0 ? 'passed' : 'failed'
}

/** What the card calls a recorded check: "Check of T1". Every other file keeps its real name. */
export function cardTitle(a: Pick<Artifact, 'name' | 'task'>, check: CheckResult | null | undefined): string {
  return check && a.task ? `Check of ${a.task}` : a.name
}

export const CHECK_QUIET = 'The check ran and printed nothing. That is normal when it passes.'

/** The type word that is always written on the badge (colour is only a second cue). */
export function typeBadge(a: Pick<Artifact, 'name' | 'kind'>, check?: CheckResult | null): { label: string; tone: string } {
  if (check) return { label: 'CHECK', tone: check === 'passed' ? 'text-success' : 'text-danger' }
  const e = ext(a.name)
  const by: Record<string, [string, string]> = {
    json: ['JSON', 'text-warning'], jsonl: ['JSON', 'text-warning'], py: ['PY', 'text-info'], md: ['MD', 'text-info'], markdown: ['MD', 'text-info'],
    log: ['LOG', 'text-text-muted'], txt: ['TXT', 'text-text-muted'], html: ['HTML', 'text-warning'], htm: ['HTML', 'text-warning'],
    png: ['PNG', 'text-brand'], jpg: ['JPG', 'text-brand'], jpeg: ['JPG', 'text-brand'], webp: ['WEBP', 'text-brand'], gif: ['GIF', 'text-brand'], svg: ['SVG', 'text-brand'],
    csv: ['CSV', 'text-success'], pdf: ['PDF', 'text-danger'], ts: ['TS', 'text-info'], js: ['JS', 'text-info'], sh: ['SH', 'text-info'], yaml: ['YAML', 'text-info'], yml: ['YAML', 'text-info'], toml: ['TOML', 'text-info'], diff: ['DIFF', 'text-info'], patch: ['DIFF', 'text-info'],
  }
  const hit = by[e]
  return hit ? { label: hit[0], tone: hit[1] } : { label: e ? e.toUpperCase().slice(0, 5) : a.kind.toUpperCase().slice(0, 5), tone: 'text-text-muted' }
}

export function TypeBadge({ a, check }: { a: Pick<Artifact, 'name' | 'kind'>; check?: CheckResult | null }) {
  const b = typeBadge(a, check)
  return <span data-testid="type-badge" className={cn('rounded border border-border bg-surface-2 px-1.5 py-px font-mono text-[10px] font-medium', b.tone)}>{b.label}</span>
}

/** Becomes true once the element scrolls near the viewport (and stays). Without IntersectionObserver it is true at once. */
function useInView() {
  const ref = useRef<HTMLDivElement>(null)
  const [seen, setSeen] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    const el = ref.current
    if (seen || !el) return
    const io = new IntersectionObserver((es) => es.some((e) => e.isIntersecting) && setSeen(true), { rootMargin: '120px' })
    io.observe(el)
    return () => io.disconnect()
  }, [seen])
  return [ref, seen] as const
}

const NO_THUMB = /^(html?|pdf|svg|gif)$/

function TextThumb({ text, name }: { text: string; name: string }) {
  const first = prose(text.replace(/\r\n/g, '\n').split('\n').slice(0, THUMB_LINES).join('\n'))
  const lang = langForFile(name)
  const markdown = lang === 'markdown'
  // Markdown is plain text lines with "#" lines bold: a file cannot draw a fake rendered heading in a card.
  const [lines, setLines] = useState<Token[][] | null>(null)
  useEffect(() => {
    if (!lang || markdown) return
    let live = true
    highlight(first, lang).then((l) => live && setLines(l)).catch(() => {})
    return () => {
      live = false
    }
  }, [first, lang, markdown])
  const rows = lines ?? first.split('\n').map((l) => [{ content: l } as Token])
  return (
    <pre aria-label={`First lines of ${visible(name)}`} data-testid="thumb-text" className="h-full overflow-hidden whitespace-pre font-mono text-[10px] leading-[14px] text-text-muted">
      {rows.map((line, i) => (
        <div key={i} className={cn('min-h-[14px] truncate', markdown && line[0]?.content.startsWith('#') && 'font-bold text-text')}>
          {line.map((t, j) => (
            <span key={j} className={tokenClass(t.color)}>
              {t.content}
            </span>
          ))}
        </div>
      ))}
    </pre>
  )
}

/** A file with no bytes: say so, never an empty box. Only a host-recorded check says passed or failed. */
function EmptyThumb({ check }: { check?: CheckResult | null }) {
  return check ? (
    <div data-testid="thumb-empty" className="flex h-full flex-col items-center justify-center gap-1 text-center text-[12px]">
      <span className={check === 'passed' ? 'font-medium text-success' : 'font-medium text-danger'}>{check === 'passed' ? 'Check passed' : 'Check failed'}</span>
      <span className="text-text-muted">{check === 'passed' ? CHECK_QUIET : 'The check printed nothing.'}</span>
    </div>
  ) : (
    <div data-testid="thumb-empty" className="flex h-full items-center justify-center text-[12px] text-text-muted">This file is empty</div>
  )
}

export function EvidenceThumb({ a, ticket, check }: { a: Artifact; ticket: string; check?: CheckResult | null }) {
  const [ref, seen] = useInView()
  const workspaces = useQuery(queries.workspaces())
  const home = workspaceOfTicket(ticket, workspaces.data ?? [])
  const e = ext(a.name)
  const eligible = a.bytes != null && a.bytes <= AUTO_OPEN_BYTES && !a.addon && !a.url && !NO_THUMB.test(e)
  const opts = artifactTextQuery(home?.id ?? '', ticket, a.name, a.sha256)
  const q = useQuery({ ...opts, queryFn: (c) => limited(() => (opts.queryFn as (c: unknown) => Promise<never>)(c)), enabled: seen && eligible && !!home })
  const d = eligible ? q.data : undefined
  let body: React.ReactNode = null
  if (d && sameDigest(d.digest, a.sha256) && d.verified && !d.truncated) {
    const m = d.media ?? d.image
    if (m) {
      // Only PNG, JPEG and WebP are pictures here; SVG and GIF stay icons.
      const url = m.type === 'png' || m.type === 'jpeg' || m.type === 'webp' ? verifiedMedia(m, a.sha256) : undefined
      if (url) body = <img src={url} alt={`Picture ${visible(a.name)}`} referrerPolicy="no-referrer" className="size-full object-cover" />
    } else if (!d.binary && d.text === '') body = <EmptyThumb check={check} />
    else if (!d.binary && d.text != null && !looksBinary(d.text)) body = <TextThumb text={d.text} name={a.name} />
  }
  // The demo (no host) carries the text inline; a real host's file is only ever shown from the digest-checked read above.
  if (!body && !isRealHost() && a.preview && !NO_THUMB.test(e)) body = <TextThumb text={a.preview} name={a.name} />
  return (
    <div ref={ref} data-testid="evidence-thumb" className="relative h-[120px] overflow-hidden rounded-md border border-border bg-bg p-2">
      {body ?? <TypeTile a={a} />}
    </div>
  )
}
