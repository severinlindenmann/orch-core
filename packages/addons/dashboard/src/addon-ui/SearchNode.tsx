// `search` (design D7): the typed text goes to a named read as `q` after a short pause; the hits are listed and a hit
// opens in the tree named by `target`. Loading, empty and error states; at most `limit` hits are drawn.
import { Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Inline } from '@/components/sign/visible'
import { Input } from '@/components/ui/input'
import { ReadFailed } from './ReadState'
import type { NodeOf } from './nodes'
import { searchResult, useViewRead } from './viewRead'
import { selectionKey, setSelection } from './viewSelection'

export const SEARCH_PAUSE_MS = 250
const MAX_QUERY = 200

export function SearchNode({ node, addon, ws }: { node: NodeOf<'search'>; addon: string; ws: string | undefined }) {
  const [text, setText] = useState('')
  const [q, setQ] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setQ(text.trim()), text.trim() === '' ? 0 : SEARCH_PAUSE_MS)
    return () => clearTimeout(t)
  }, [text])
  const res = useViewRead(addon, node.read, { q }, searchResult, q !== '')
  const clear = () => {
    setText('')
    setQ('')
  }
  const open = (id: string) => {
    if (node.target) setSelection(selectionKey(ws, addon, node.target), id)
    clear()
  }
  const waiting = q !== '' && (res.isPending || text.trim() !== q)
  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-text-muted" aria-hidden />
        <Input
          type="search"
          role="searchbox"
          aria-label={node.label}
          placeholder={node.placeholder ?? node.label}
          value={text}
          maxLength={MAX_QUERY}
          autoComplete="off"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Escape' && clear()}
          className="pl-8 pr-8 [&::-webkit-search-cancel-button]:hidden"
        />
        {text && (
          <button type="button" aria-label="Clear search" onClick={clear} className="absolute right-1.5 top-1/2 grid size-6 -translate-y-1/2 place-items-center rounded-sm text-text-muted hover:text-text focus-visible:outline-2 focus-visible:outline-ring">
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>
      {q !== '' && (
        <div className="rounded-lg border border-border bg-surface p-1.5" aria-live="polite">
          {waiting && !res.isError ? (
            <p role="status" className="px-2 py-2 text-[13px] text-text-muted">Searching…</p>
          ) : res.isError ? (
            <ReadFailed what="the search" onRetry={() => void res.refetch()} />
          ) : res.data && res.data.results.length === 0 ? (
            <p className="px-2 py-2 text-[13px] text-text-muted">
              Nothing matches “<Inline>{q}</Inline>”.
            </p>
          ) : res.data ? (
            <>
              <ul aria-label="Search results" className="divide-y divide-border">
                {res.data.results.slice(0, node.limit).map((r) => (
                  <li key={r.id}>
                    <button type="button" onClick={() => open(r.id)} className="flex w-full min-w-0 flex-col items-start gap-0.5 rounded-md px-2 py-2 text-left hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring">
                      <span className="max-w-full truncate text-[13px] font-medium text-text">
                        <Inline>{r.label}</Inline>
                      </span>
                      {r.snippet && r.snippet !== r.label && (
                        <span className="line-clamp-2 max-w-full text-[12px] text-text-muted">
                          <Inline>{r.snippet}</Inline>
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
              {(res.data.results.length > node.limit || res.data.cut) && (
                <p className="px-2 pt-2 text-[12px] text-text-muted">
                  {res.data.results.length > node.limit ? `${res.data.results.length - node.limit} more matches. ` : ''}
                  {res.data.cut ? 'The search stopped early; ' : ''}Try a more specific word to narrow it down.
                </p>
              )}
            </>
          ) : null}
        </div>
      )}
    </div>
  )
}
