// The lazy folder tree: a folder is read when it is opened. Locked entries are SHOWN (with a lock), never hidden and never
// opened; links are shown as links and never followed.
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, File as FileIcon, Folder, FolderGit2, Link2, Lock } from 'lucide-react'
import { explorerQueries, join, type Entry } from '@/api/explorer'
import { LoadFailed } from '@/components/LoadFailed'
import { cn } from '@/lib/utils'

const SIZE = (n: number | undefined) => (n === undefined ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`)

function Row({ depth, selected, children, ...rest }: { depth: number; selected?: boolean; children: React.ReactNode } & React.HTMLAttributes<HTMLElement> & { onClick?: () => void; disabled?: boolean }) {
  const style = { paddingLeft: `${8 + depth * 14}px` }
  const cls = cn('flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-[13px]', selected ? 'bg-brand-soft' : 'hover:bg-surface-hover')
  if (rest.onClick) {
    return (
      <button type="button" style={style} className={cls} aria-current={selected ? 'true' : undefined} onClick={rest.onClick} {...(rest['aria-label'] ? { 'aria-label': rest['aria-label'] } : {})}>
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

export function Folder_({ ws, root, path, depth, ...p }: TreeProps & { root: string; path: string; depth: number }) {
  const q = useQuery(explorerQueries.tree(ws, root, path))
  if (q.isError) return <LoadFailed what="this folder" onRetry={() => void q.refetch()} />
  if (!q.data) return <p className="px-3 py-1 text-[12px] text-text-faint" style={{ paddingLeft: `${8 + depth * 14}px` }}>Loading…</p>
  return (
    <ul role={depth === 0 ? 'tree' : 'group'} aria-label={depth === 0 ? 'Files' : undefined}>
      {q.data.entries.map((e) => (
        <li key={e.name} role="treeitem" aria-selected={p.selected?.root === root && p.selected.path === join(path, e.name)}>
          <Item ws={ws} root={root} dir={path} entry={e} depth={depth} {...p} />
        </li>
      ))}
      {q.data.entries.length === 0 && <li className="px-3 py-1 text-[12px] text-text-faint">Empty folder</li>}
      {q.data.more && <li className="px-3 py-1 text-[12px] text-text-faint">This folder has more entries than are listed. Use the search to find a file.</li>}
    </ul>
  )
}

function Item({ ws, root, dir, entry, depth, ...p }: TreeProps & { root: string; dir: string; entry: Entry; depth: number }) {
  const path = join(dir, entry.name)
  const label = <bdi className="min-w-0 flex-1 truncate [unicode-bidi:isolate]">{entry.label}</bdi>
  if (entry.locked) {
    return (
      <Row depth={depth} aria-label={`${entry.label}, locked: ${entry.locked}`}>
        <Lock className="size-3.5 shrink-0" aria-hidden />
        {label}
        <span className="shrink-0 text-[11px] text-text-faint">Locked</span>
      </Row>
    )
  }
  if (entry.kind === 'dir') {
    const key = `${root}:${path}`
    const isOpen = p.open.has(key)
    return (
      <>
        <Row depth={depth} onClick={() => p.toggle(key)} aria-label={`${isOpen ? 'Close' : 'Open'} folder ${entry.label}`}>
          {isOpen ? <ChevronDown className="size-3.5 shrink-0" aria-hidden /> : <ChevronRight className="size-3.5 shrink-0" aria-hidden />}
          {entry.repo ? <FolderGit2 className="size-3.5 shrink-0 text-brand" aria-hidden /> : <Folder className="size-3.5 shrink-0 text-text-muted" aria-hidden />}
          {label}
          {entry.repo && <span className="shrink-0 text-[11px] text-text-faint">repository</span>}
        </Row>
        {isOpen && <Folder_ ws={ws} root={root} path={path} depth={depth + 1} {...p} />}
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
