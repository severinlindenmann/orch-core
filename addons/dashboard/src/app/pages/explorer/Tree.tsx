// The lazy folder tree: a folder is read when it is opened. Locked entries are SHOWN (with a lock), never hidden and never
// opened; links are shown as links and never followed. Folders are disclosure buttons (aria-expanded) in nested lists;
// the arrow keys move between rows (Right opens or enters a folder, Left closes it or goes to its parent).
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, File as FileIcon, Folder, FolderGit2, Link2, Lock } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { explorerQueries, join, lockedText, type Entry } from '@/api/explorer'
import { LoadFailed } from '@/components/LoadFailed'
import { cn } from '@/lib/utils'

const SIZE = (n: number | undefined) => (n === undefined ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`)

function Row({ depth, selected, expanded, children, ...rest }: { depth: number; selected?: boolean; expanded?: boolean; children: React.ReactNode } & React.HTMLAttributes<HTMLElement> & { onClick?: () => void; disabled?: boolean }) {
  const style = { paddingLeft: `${8 + depth * 14}px` }
  const cls = cn('flex min-h-9 w-full items-center gap-1.5 rounded px-2 py-1 text-left text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-brand', selected ? 'bg-brand-soft font-medium' : 'hover:bg-surface-hover')
  const ref = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView?.({ block: 'nearest' })
  }, [selected])
  if (rest.onClick) {
    return (
      <button ref={ref} type="button" style={style} className={cls} data-tree-row aria-current={selected ? 'true' : undefined} aria-expanded={expanded} onClick={rest.onClick} {...(rest['aria-label'] ? { 'aria-label': rest['aria-label'] } : {})}>
        {children}
      </button>
    )
  }
  return (
    <div style={style} className={cn(cls, 'text-text-muted')} {...(rest['aria-label'] ? { 'aria-label': rest['aria-label'] } : {})}>
      {children}
    </div>
  )
}

export interface TreeProps {
  ws: string
  open: ReadonlySet<string>
  toggle: (key: string) => void
  selected: { root: string; path: string } | null
  onPick: (root: string, path: string) => void
}

/** Arrow keys over the visible rows of one tree: Up/Down/Home/End move, Right opens or enters a folder, Left closes it or goes to its parent. */
function onTreeKey(e: React.KeyboardEvent<HTMLElement>) {
  const target = (e.target as HTMLElement).closest<HTMLElement>('[data-tree-row]')
  if (!target) return
  const rows = [...e.currentTarget.querySelectorAll<HTMLElement>('[data-tree-row]')]
  const at = rows.indexOf(target)
  const go = (i: number) => { e.preventDefault(); rows[Math.max(0, Math.min(rows.length - 1, i))]?.focus() }
  const expanded = target.getAttribute('aria-expanded')
  switch (e.key) {
    case 'ArrowDown': return go(at + 1)
    case 'ArrowUp': return go(at - 1)
    case 'Home': return go(0)
    case 'End': return go(rows.length - 1)
    case 'ArrowRight':
      if (expanded === 'false') { e.preventDefault(); target.click() } else if (expanded === 'true') go(at + 1)
      return
    case 'ArrowLeft':
      if (expanded === 'true') { e.preventDefault(); target.click(); return }
      {
        // the parent folder: the nearest earlier row that is less indented
        const depthOf = (el: HTMLElement) => parseInt(el.style.paddingLeft || '0', 10)
        for (let i = at - 1; i >= 0; i--) if (depthOf(rows[i]) < depthOf(target)) return go(i)
      }
  }
}

export function FolderList({ ws, root, path, depth, ...p }: TreeProps & { root: string; path: string; depth: number }) {
  const q = useQuery(explorerQueries.tree(ws, root, path))
  if (q.isError) return <LoadFailed what="this folder" onRetry={() => void q.refetch()} />
  if (!q.data) return <p role="status" className="px-3 py-1 text-[12px] text-text-faint" style={{ paddingLeft: `${8 + depth * 14}px` }}>Loading…</p>
  return (
    <ul aria-label={depth === 0 ? 'Files' : undefined} onKeyDown={depth === 0 ? onTreeKey : undefined} data-tree-root={depth === 0 ? '' : undefined}>
      {q.data.entries.map((e, i, all) => (
        <li key={e.name}>
          {/* The reason is said once per run of entries locked for the same reason, not under every one */}
          <Item ws={ws} root={root} dir={path} entry={e} depth={depth} sameReasonAsAbove={!!e.locked && all[i - 1]?.locked === e.locked} {...p} />
        </li>
      ))}
      {q.data.entries.length === 0 && <li className="px-3 py-1 text-[12px] text-text-faint">Empty folder</li>}
      {q.data.more && <li className="px-3 py-1 text-[12px] text-text-faint">This folder has more entries than are listed. Use the search to find a file.</li>}
    </ul>
  )
}

function Item({ ws, root, dir, entry, depth, sameReasonAsAbove, ...p }: TreeProps & { root: string; dir: string; entry: Entry; depth: number; sameReasonAsAbove?: boolean }) {
  const path = join(dir, entry.name)
  const label = <bdi className="min-w-0 flex-1 truncate [unicode-bidi:isolate]">{entry.label}</bdi>
  if (entry.locked) {
    return (
      <>
        <Row depth={depth} aria-label={`${entry.label}, locked: ${entry.locked}`}>
          <Lock className="size-3.5 shrink-0" aria-hidden />
          {label}
          <span className="shrink-0 text-[11px] text-text-faint">Locked</span>
        </Row>
        {!sameReasonAsAbove && <p className="pb-1 pr-2 text-[11px] text-text-faint" style={{ paddingLeft: `${8 + depth * 14 + 22}px` }} data-testid="why-locked">Why locked? {lockedText(entry.locked)}</p>}
      </>
    )
  }
  if (entry.kind === 'dir') {
    const key = `${root}:${path}`
    const isOpen = p.open.has(key)
    return (
      <>
        <Row depth={depth} expanded={isOpen} onClick={() => p.toggle(key)} aria-label={`Folder ${entry.label}`}>
          {isOpen ? <ChevronDown className="size-3.5 shrink-0" aria-hidden /> : <ChevronRight className="size-3.5 shrink-0" aria-hidden />}
          {entry.repo ? <FolderGit2 className="size-3.5 shrink-0 text-brand" aria-hidden /> : <Folder className="size-3.5 shrink-0 text-text-muted" aria-hidden />}
          {label}
          {entry.repo && <span className="shrink-0 text-[11px] text-text-faint">repository</span>}
        </Row>
        {isOpen && <FolderList ws={ws} root={root} path={path} depth={depth + 1} {...p} />}
      </>
    )
  }
  if (entry.kind === 'file') {
    return (
      <Row depth={depth} selected={p.selected?.root === root && p.selected.path === path} onClick={() => p.onPick(root, path)} aria-label={`File ${entry.label}`}>
        <FileIcon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
        {label}
        <span className="shrink-0 text-[11px] text-text-faint">{SIZE(entry.size)}</span>
      </Row>
    )
  }
  return (
    <Row depth={depth} aria-label={`${entry.label}, ${entry.kind === 'link' ? 'a link' : 'not a regular file'}`}>
      <Link2 className="size-3.5 shrink-0" aria-hidden />
      {label}
      <span className="shrink-0 text-[11px] text-text-faint">{entry.kind === 'link' ? 'Link' : 'Other'}</span>
    </Row>
  )
}
