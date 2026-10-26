import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { appendItems, focusApi, focusQueries, type ChatItem } from '@/api/focus'
import type { Run } from '@/api/runs'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

export const POLL_MS = 1500
interface Feed { items: ChatItem[]; next: string | null; available: boolean; reason: string | null; eof: boolean }

/** The transcript of a run, read from a cursor: every poll returns only what is new and is appended here. */
export function useTranscript(ws: string, run: Run, child?: string) {
  const feed = useRef<Feed>({ items: [], next: null, available: false, reason: null, eof: true })
  const live = run.state === 'running' || run.state === 'starting'
  return useQuery({
    queryKey: focusQueries.transcriptKey(ws, run.run, child),
    queryFn: async (): Promise<Feed> => {
      const got = await focusApi.transcript(ws, run.run, { after: feed.current.next, child })
      feed.current = {
        items: appendItems(feed.current.items, got.items),
        next: got.next ?? feed.current.next, available: got.available, reason: got.reason, eof: got.eof,
      }
      return feed.current
    },
    retry: false,
    refetchInterval: (q) => (live || q.state.data?.eof === false ? POLL_MS : false),
  })
}

const time = (ts: string | null) => (ts ? new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')

function Item({ item }: { item: ChatItem }) {
  const text = item.text ?? ''
  if (item.kind === 'status') return <p className="text-center text-xs text-text-faint">{text}</p>
  if (item.kind === 'tool') return <p className="font-mono text-xs text-text-muted" data-kind="tool">{'> '}{text}{item.files?.length ? <span className="text-text-faint"> ({item.files.length} {item.files.length === 1 ? 'file' : 'files'})</span> : null}</p>
  if (item.kind === 'result') return <p className={`truncate font-mono text-xs ${item.error ? 'text-danger' : 'text-text-faint'}`} data-kind="result" title={text}>{item.error ? 'Error: ' : ''}{text.split('\n')[0]}</p>
  if (item.kind === 'question') return <div className="rounded-lg border border-brand/30 bg-brand-soft p-3 text-sm" data-kind="question">
    <p className="text-xs font-medium text-brand">Question for you</p>
    <p className="whitespace-pre-wrap">{text}</p>
    {item.options?.length ? <ul className="mt-1 list-disc pl-5 text-text-muted">{item.options.map((o) => <li key={o}>{o}</li>)}</ul> : null}
  </div>
  const mine = item.role === 'user'
  return <div className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
    <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${mine ? 'bg-brand-soft' : 'bg-surface'}`}>
      <p className="text-xs text-text-faint">{mine ? 'You' : 'Agent'} {time(item.ts)}</p>
      <p className="whitespace-pre-wrap break-words">{text}{item.truncated ? ' ...' : ''}</p>
    </div>
  </div>
}

export function Chat({ ws, run, canSend, onSend, onTake }: {
  ws: string
  run: Run
  /** The terminal holds the write lease: the composer writes through it. */
  canSend: boolean
  onSend: (text: string) => boolean
  onTake: () => void
}) {
  const feed = useTranscript(ws, run)
  const [draft, setDraft] = useState('')
  const [failed, setFailed] = useState(false)
  const list = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const items = feed.data?.items ?? []
  useLayoutEffect(() => {
    const el = list.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [items.length])
  useEffect(() => { setFailed(false) }, [canSend])
  const running = run.state === 'running'
  function send() {
    const text = draft.trim()
    if (!text) return
    if (onSend(text)) { setDraft(''); setFailed(false) } else setFailed(true)
  }
  return <section aria-label="Chat" className="absolute inset-0 flex min-h-0 flex-col bg-bg">
    <div ref={list} role="log" aria-live="polite" className="min-h-0 flex-1 space-y-2 overflow-y-auto p-4"
      onScroll={(e) => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }}>
      {feed.isError ? <p role="alert" className="text-sm text-danger">Could not load the chat.</p>
        : feed.isPending ? <p role="status" className="text-sm text-text-muted">Loading chat...</p>
        : !feed.data.available ? <p role="status" className="text-sm text-text-muted">{feed.data.reason === 'transcript not found yet' ? 'Waiting for the first message...' : feed.data.reason ?? 'No chat is available for this session.'}</p>
        : items.length === 0 ? <p role="status" className="text-sm text-text-muted">Nothing said yet.</p>
        : items.map((item) => <Item key={item.id} item={item} />)}
    </div>
    <form className="flex shrink-0 items-end gap-2 border-t border-border p-3" onSubmit={(e) => { e.preventDefault(); send() }}>
      <Textarea aria-label="Message the agent" rows={1} value={draft} disabled={!running} placeholder={running ? 'Intervene or add context...' : 'This session has ended'}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }} />
      {running && !canSend ? <Button type="button" variant="outline" size="sm" onClick={onTake}>Take control</Button> : null}
      <Button type="submit" size="sm" disabled={!running || !canSend || !draft.trim()}>Send</Button>
      {failed && <p role="alert" className="sr-only">The message was not sent. Take control of the terminal first.</p>}
    </form>
    {running && !canSend && <p className="px-3 pb-2 text-xs text-text-muted">Messages go through the terminal. Take control of it to send.</p>}
  </section>
}
