// `tree` (design D7): items listed by a named read, children loaded lazily, one item open in a pane beside the tree.
// WAI-ARIA tree: roving tabindex, Up/Down/Home/End move, Right opens (or enters), Left closes (or goes to the parent),
// Enter/Space open the item. On a phone the tree and the pane take turns, with "All items" to go back.
import { ChevronRight, ArrowLeft } from 'lucide-react'
import { createContext, useContext, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { ApiError } from '@/api/types'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { fmtDateTime } from '@/lib/time'
import { ReadFailed, ReadLoading } from './ReadState'
import { SafeMarkdown } from './SafeMarkdown'
import type { NodeOf } from './nodes'
import { detailResult, treeResult, useViewRead, type TreeEntry } from './viewRead'
import { selectionKey, useSelection } from './viewSelection'

/** Rows drawn before "Show more": a long folder never becomes one endless list. */
export const TREE_PAGE = 100

interface Ctx {
  addon: string
  read: string
  selected: string | null
  tabbable: string | null
  open: Set<string>
  toggle: (id: string) => void
  select: (id: string) => void
}
const TreeCtx = createContext<Ctx | null>(null)

function Row({ entry, level, parent, gutter }: { entry: TreeEntry; level: number; parent: string | null; gutter: boolean }) {
  const c = useContext(TreeCtx) as Ctx
  const isOpen = c.open.has(entry.id)
  const on = c.selected === entry.id
  return (
    <>
      <div
        role="treeitem"
        aria-level={level}
        aria-selected={on}
        aria-expanded={entry.children ? isOpen : undefined}
        tabIndex={c.tabbable === entry.id ? 0 : -1}
        data-id={entry.id}
        data-parent={parent ?? undefined}
        title={entry.hint}
        onClick={() => c.select(entry.id)}
        style={{ paddingLeft: `${0.5 + (level - 1) * 1}rem` }}
        className={cn(
          'flex min-h-9 cursor-pointer items-center gap-1.5 rounded-md border-l-2 border-transparent py-1.5 pr-2 text-[13px] text-text outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand md:min-h-8',
          on && 'border-brand bg-surface-2 font-medium',
        )}
      >
        {entry.children ? (
          <button
            type="button"
            tabIndex={-1}
            aria-hidden
            onClick={(e) => {
              e.stopPropagation()
              c.toggle(entry.id)
            }}
            className="grid size-5 shrink-0 place-items-center rounded-sm text-text-muted hover:text-text"
          >
            <ChevronRight className={cn('size-4 transition-transform', isOpen && 'rotate-90')} />
          </button>
        ) : gutter ? (
          <span aria-hidden className="size-5 shrink-0" />
        ) : null}
        <span className="min-w-0 flex-1 truncate">
          <Inline>{entry.label}</Inline>
        </span>
        {entry.badge && <span className="shrink-0 text-[11px] text-text-muted">{entry.badge}</span>}
      </div>
      {entry.children && isOpen && <Children parent={entry.id} level={level + 1} />}
    </>
  )
}

/** The lazily read children of an open item. */
function Children({ parent, level }: { parent: string; level: number }) {
  const c = useContext(TreeCtx) as Ctx
  const q = useViewRead(c.addon, c.read, { parent }, treeResult)
  const pad = { paddingLeft: `${0.5 + (level - 1) * 1 + 1.5}rem` }
  if (q.isPending) return <div role="status" aria-label="Loading" style={pad} className="py-1.5 text-[12px] text-text-muted">Loading…</div>
  if (q.isError) {
    return (
      <div role="alert" style={pad} className="flex items-center gap-2 py-1 text-[12px] text-text-muted">
        Could not load this folder.
        <Button variant="ghost" size="sm" onClick={() => void q.refetch()}>
          Retry
        </Button>
      </div>
    )
  }
  return (
    <>
      {q.data.nodes.map((n) => (
        <Row key={n.id} entry={n} level={level} parent={parent} gutter={q.data.nodes.some((x) => x.children)} />
      ))}
    </>
  )
}

/** The text without its first line when that line is `# <title>` (the header shows the title already). */
export function withoutTitle(text: string, title: string): string {
  const t = text.trimStart()
  const nl = t.indexOf('\n')
  const first = (nl < 0 ? t : t.slice(0, nl)).trim()
  return first === `# ${title}` ? (nl < 0 ? '' : t.slice(nl + 1)) : text
}

function Detail({ addon, cfg, id, onBack }: { addon: string; cfg: NonNullable<NodeOf<'tree'>['detail']>; id: string; onBack: () => void }) {
  const q = useViewRead(addon, cfg.read, { id }, detailResult)
  if (q.isPending) return <ReadLoading label="Reading" rows={3} />
  if (q.isError) {
    const gone = q.error instanceof ApiError && q.error.status === 404
    return gone ? (
      <div role="alert" className="space-y-2 text-[13px]">
        <p className="text-text-muted">This item is no longer there.</p>
        <Button variant="outline" size="sm" onClick={onBack}>
          Back to the list
        </Button>
      </div>
    ) : (
      <ReadFailed what="this item" onRetry={() => void q.refetch()} />
    )
  }
  const title = q.data[cfg.title]
  const text = q.data[cfg.text]
  const updated = q.data.updated
  const by = q.data.by
  // A page that starts with its own title as a "# " heading would show it twice: the header already says it.
  const body = text && title ? withoutTitle(text, title) : text
  return (
    <article className="space-y-3">
      <header className="space-y-0.5">
        {title && (
          <h2 className="text-lg font-semibold tracking-tight">
            <Inline>{title}</Inline>
          </h2>
        )}
        {updated && (
          <p className="text-[12px] text-text-muted">
            Updated {fmtDateTime(updated)}
            {by ? (
              <>
                {' '}
                by <Inline>{by}</Inline>
              </>
            ) : null}
          </p>
        )}
      </header>
      {body === null || body === undefined ? (
        <p className="text-[13px] text-text-muted">{cfg.empty ?? 'Nothing to show here.'}</p>
      ) : body.trim() === '' ? (
        <p className="text-[13px] text-text-muted">This page is empty.</p>
      ) : (
        <div className="max-w-[72ch]">
          <SafeMarkdown text={body} />
        </div>
      )}
    </article>
  )
}

const wide = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(min-width: 768px)').matches

export function TreeNode({ node, addon, ws, renderOffer }: { node: NodeOf<'tree'>; addon: string; ws: string | undefined; renderOffer: (offer: { label: string; action: string }) => ReactNode }) {
  const key = selectionKey(ws, addon, node.id)
  const [selected, setSelected] = useSelection(key)
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const [count, setCount] = useState(TREE_PAGE)
  const [focused, setFocused] = useState<string | null>(null)
  const box = useRef<HTMLDivElement>(null)
  const roots = useViewRead(addon, node.read, {}, treeResult)
  const first = roots.data?.nodes[0]?.id ?? null

  // Next to the tree the pane is never empty: the first item opens by itself. On a phone the list comes first.
  useEffect(() => {
    if (node.detail && selected === null && first !== null && wide()) setSelected(first)
  }, [first, selected, node.detail, setSelected])

  const ctx = useMemo<Ctx>(
    () => ({
      addon,
      read: node.read,
      selected,
      tabbable: focused ?? selected ?? first,
      open,
      toggle: (id) => setOpen((o) => (o.has(id) ? new Set([...o].filter((x) => x !== id)) : new Set(o).add(id))),
      select: (id) => {
        setFocused(id)
        setSelected(id)
      },
    }),
    [addon, node.read, selected, focused, first, open, setSelected],
  )

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(box.current?.querySelectorAll<HTMLElement>('[role="treeitem"]') ?? [])
    const i = items.indexOf(e.target as HTMLElement)
    if (i < 0) return
    const cur = items[i]
    const id = cur.dataset.id as string
    const to = (el?: HTMLElement) => {
      if (!el) return
      e.preventDefault()
      el.focus()
    }
    switch (e.key) {
      case 'ArrowDown':
        return to(items[i + 1])
      case 'ArrowUp':
        return to(items[i - 1])
      case 'Home':
        return to(items[0])
      case 'End':
        return to(items[items.length - 1])
      case 'ArrowRight':
        if (cur.getAttribute('aria-expanded') === 'false') {
          e.preventDefault()
          ctx.toggle(id)
        } else if (cur.getAttribute('aria-expanded') === 'true') to(items[i + 1])
        return
      case 'ArrowLeft':
        if (cur.getAttribute('aria-expanded') === 'true') {
          e.preventDefault()
          ctx.toggle(id)
        } else to(items.find((x) => x.dataset.id === cur.dataset.parent))
        return
      case 'Enter':
      case ' ':
        e.preventDefault()
        ctx.select(id)
    }
  }

  let list: ReactNode
  if (roots.isPending) list = <ReadLoading label="Loading" />
  else if (roots.isError) list = <ReadFailed what={node.label.toLowerCase()} onRetry={() => void roots.refetch()} />
  else if (roots.data.nodes.length === 0) {
    const d = roots.data
    list = (
      <div data-testid="tree-empty" className="space-y-3 p-3">
        <p className="text-[13px] text-text-muted">{d.message ?? node.empty ?? 'Nothing here.'}</p>
        {d.offer && renderOffer(d.offer)}
      </div>
    )
  } else {
    const d = roots.data
    const shown = d.nodes.slice(0, count)
    list = (
      <>
        <div ref={box} role="tree" aria-label={node.label} onKeyDown={onKeyDown} onFocus={(e) => setFocused((e.target as HTMLElement).dataset.id ?? null)} className="space-y-0.5">
          {shown.map((n) => (
            <Row key={n.id} entry={n} level={1} parent={null} gutter={d.nodes.some((x) => x.children)} />
          ))}
        </div>
        {shown.length < d.nodes.length && (
          <Button variant="ghost" size="sm" className="mt-1 w-full" onClick={() => setCount((c) => c + TREE_PAGE)}>
            Show more ({d.nodes.length - shown.length} left)
          </Button>
        )}
        {(d.cut || d.total > d.nodes.length) && <p className="px-2 pt-2 text-[12px] text-text-muted">Showing the first {d.nodes.length} of {d.total}.</p>}
      </>
    )
  }

  // The pane has nothing to say while the list is loading, failed or empty: the list's own message stands alone.
  const pane = !!node.detail && !!roots.data && roots.data.nodes.length > 0
  return (
    <TreeCtx.Provider value={ctx}>
      <div className={cn('grid min-w-0 items-start gap-4', pane && 'md:grid-cols-[minmax(13rem,17rem)_minmax(0,1fr)]')}>
        <div className={cn('min-w-0 rounded-lg border border-border bg-surface p-1.5 md:max-h-[calc(100dvh-13rem)] md:overflow-y-auto', pane && selected && 'max-md:hidden')}>{list}</div>
        {pane && node.detail && (
          <section aria-label="Open item" className={cn('min-w-0 rounded-lg border border-border bg-surface p-4 md:p-5', !selected && 'max-md:hidden')}>
            {selected ? (
              <>
                <Button variant="ghost" size="sm" className="-ml-2 mb-2 md:hidden" onClick={() => setSelected(null)}>
                  <ArrowLeft className="size-4" aria-hidden /> All items
                </Button>
                <Detail key={selected} addon={addon} cfg={node.detail} id={selected} onBack={() => setSelected(null)} />
              </>
            ) : (
              <p className="text-[13px] text-text-muted">Choose an item to read it.</p>
            )}
          </section>
        )}
      </div>
    </TreeCtx.Provider>
  )
}
