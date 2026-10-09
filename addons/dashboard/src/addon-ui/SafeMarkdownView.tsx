import { useEffect, useId, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { ChevronDown } from 'lucide-react'

const isHttp = (href?: string) => !!href && /^https?:\/\//i.test(href)

export interface Heading {
  id: string
  level: number
  text: string
}

interface HastNode {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}
const textOf = (n: HastNode): string => (n.type === 'text' ? (n.value ?? '') : (n.children ?? []).map(textOf).join(''))

/** `Loader query` -> `loader-query`; the same text twice gets `-2`, `-3` (core's ids, never the addon's). */
export function slugOf(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'section'
}

/**
 * Gives every heading an id made by core (`addon-h-<slug>`, de-duplicated) and records the headings. Runs after the
 * sanitizer, so an id or anchor in the addon's markdown can never survive: only these ids exist.
 */
function headingIds(into: Heading[], opts: { toc: boolean; prefix: string }) {
  return () => (tree: HastNode) => {
    into.length = 0 // a re-render (or React's double render) starts the list again
    const seen = new Map<string, number>()
    const walk = (n: HastNode) => {
      if (n.type !== 'element') {
        n.children?.forEach(walk)
        return
      }
      const isHeading = !!n.tagName && /^h[1-6]$/.test(n.tagName)
      if (n.properties && 'id' in n.properties) {
        const { id: _id, ...rest } = n.properties // an id the addon's text produced (footnotes, anchors) never survives
        n.properties = rest
      }
      if (isHeading && opts.toc) {
        const text = textOf(n).trim()
        const base = `${opts.prefix}${slugOf(text)}`
        const count = (seen.get(base) ?? 0) + 1
        seen.set(base, count)
        const id = count === 1 ? base : `${base}-${count}`
        n.properties = { ...n.properties, id }
        into.push({ id, level: Number(n.tagName![1]), text })
        return
      }
      n.children?.forEach(walk)
    }
    walk(tree)
  }
}

/** Several markdown nodes with a toc on one page must not share ids: the first live one is `addon-h-`, the next `addon-h2-`, ... */
const liveToc: string[] = []

const tocItems = (headings: Heading[]) => headings.filter((h) => h.level >= 2 && h.level <= 3)

function TocLinks({ items }: { items: Heading[] }) {
  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ block: 'start' })
  return (
    <ul className="space-y-1">
      {items.map((h) => (
        <li key={h.id} className={h.level === 3 ? 'pl-3' : undefined}>
          <button type="button" onClick={() => go(h.id)} className="text-left text-[12px] text-text-muted hover:text-text hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
            {h.text}
          </button>
        </li>
      ))}
    </ul>
  )
}

/** Below 1280 px: a collapsed disclosure above the text. */
function TocDisclosure({ items }: { items: Heading[] }) {
  return (
    <details className="group rounded-md border border-border px-3 py-2 xl:hidden">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[12px] text-text-muted">
        <ChevronDown className="size-3.5 transition-transform group-open:rotate-180" aria-hidden />
        On this page
      </summary>
      <nav aria-label="On this page (short)" className="mt-2">
        <TocLinks items={items} />
      </nav>
    </details>
  )
}

/** From 1280 px: a narrow column beside the text. */
function TocColumn({ items }: { items: Heading[] }) {
  return (
    <nav aria-label="On this page" className="hidden xl:sticky xl:top-4 xl:block">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-text-faint">On this page</div>
      <TocLinks items={items} />
    </nav>
  )
}

/**
 * Markdown from an addon. Raw HTML is never interpreted (no rehype-raw), the output is sanitized,
 * links are only http(s) and open in a new tab, and images are never loaded (an alt-text placeholder instead).
 * With `toc`, headings get core-made ids and "On this page" links to them.
 */
export function SafeMarkdownView({ text, toc = false }: { text: string; toc?: boolean }) {
  const found = useRef<Heading[]>([])
  const [headings, setHeadings] = useState<Heading[]>([])
  const owner = useId()
  const [slot, setSlot] = useState(0)
  useEffect(() => {
    if (!toc) return
    liveToc.push(owner)
    setSlot(liveToc.indexOf(owner))
    return () => void liveToc.splice(liveToc.indexOf(owner), 1)
  }, [toc, owner])
  const prefix = slot === 0 ? 'addon-h-' : `addon-h${slot + 1}-`
  const plugin = useMemo(() => headingIds(found.current, { toc, prefix }), [toc, prefix])
  found.current.length = 0
  useEffect(() => {
    if (!toc) return
    const next = [...found.current]
    setHeadings((cur) => (JSON.stringify(cur) === JSON.stringify(next) ? cur : next))
  }, [text, toc, prefix])
  const body = (
    <div className="addon-md text-[13px] leading-relaxed text-text [&_blockquote]:border-l-2 [&_blockquote]:border-border-strong [&_blockquote]:pl-3 [&_blockquote]:text-text-muted [&_code]:rounded [&_code]:bg-surface-3 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px] [&_h1]:mb-2 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:mb-1.5 [&_h2]:mt-4 [&_h2]:scroll-mt-4 [&_h2]:text-[15px] [&_h2]:font-semibold [&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:scroll-mt-4 [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:bg-surface-3 [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_table]:my-2 [&_table]:w-full [&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-border [&_th]:bg-surface-2 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize, plugin]}
        urlTransform={(url) => (isHttp(url) ? url : '')}
        components={{
          a: ({ href, children }) =>
            isHttp(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-brand underline-offset-2 hover:underline">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span className="text-text-faint">[image: {alt || 'not loaded'}]</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
  if (!toc) return body
  const items = tocItems(headings)
  if (items.length < 2) return body
  return (
    <div className="gap-8 space-y-3 xl:grid xl:grid-cols-[minmax(0,1fr)_13rem] xl:space-y-0">
      <div className="min-w-0 space-y-3">
        <TocDisclosure items={items} />
        {body}
      </div>
      <TocColumn items={items} />
    </div>
  )
}
