// Core-rendered terminal; the declarative node retains the pty capability gate.
import '@xterm/xterm/css/xterm.css'
import { FitAddon } from '@xterm/addon-fit'
import { SearchAddon } from '@xterm/addon-search'
import { Terminal } from '@xterm/xterm'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { TerminalSessionView } from '@/api/terminals'
import { useAddonStates } from '@/addon-ui/slots'
import { Skeleton } from '@/components/ui/skeleton'
import { useRole } from '../useRole'
import { useWorkspace } from '../workspace'
import { clean, createShell, replay } from './fakePty'
import { SessionList } from './SessionList'
import { TerminalHeader } from './TerminalHeader'

const token = (el: HTMLElement, name: string) => getComputedStyle(el).getPropertyValue(name).trim() || undefined
const NO_LINKS = { activate: () => {}, hover: () => {}, leave: () => {} }
// A global budget includes page and ticket-rail terminals. Eviction disposes synchronously before mounting another.
const live = new Map<symbol, () => void>()
const saved = new WeakMap<QueryClient, Map<string, { output: string; viewport: number; following: boolean }>>()

export default function TerminalView({ addon, session, fallback, placement = 'page' }: { addon: string; session: string; fallback: ReactNode; placement?: 'page' | 'rail' }) {
  const { workspace } = useWorkspace()
  const { [addon]: state } = useAddonStates(workspace?.id, [addon])
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const role = useRole()
  const qc = useQueryClient()
  const [wide, setWide] = useState(() => window.innerWidth >= 1280)
  const [picker, setPicker] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const media = window.matchMedia('(min-width: 1280px)')
    const change = () => setWide(media.matches)
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])
  if (!workspace || !state || !me.data || !role) return <Skeleton className="h-64 w-full" />
  if (session === 'none') return <div className="rounded-md border border-border bg-bg px-3 py-6 text-center text-[13px] text-text-muted">No terminal is open. Start one with New terminal.</div>
  const sessions = (state.sessions as TerminalSessionView[] | undefined) ?? []
  const s = sessions.find((x) => x.id === session)
  if (!s) return <>{fallback}</>
  const interactive = s.interactive && s.status === 'running' && s.kind === 'person' && s.owner === me.data.person && can(role, 'addon.action')
  const fontSize = Number((state.settings as { font_size?: number } | undefined)?.font_size) || 13
  const action = async (name: string, id?: string) => {
    try {
      await api.runAddonAction(workspace.id, addon, name, id ? { session: id } : {})
      await qc.invalidateQueries({ queryKey: ['addon-state'] })
      setError('')
    } catch (e) { setError(e instanceof Error ? e.message : 'Terminal action failed.') }
  }
  const list = <SessionList sessions={sessions} selected={s.id} onSelect={(id) => { void action('open', id); setPicker(false) }} />
  return <div className="min-w-0">
    {error && <p role="alert">{error}</p>}
    <div className={`grid min-w-0 gap-3 ${placement === 'page' && wide ? 'grid-cols-[260px_minmax(0,1fr)]' : 'grid-cols-1'}`}>
      {placement === 'page' && wide && <aside className="max-h-[calc(100vh-188px)] overflow-y-auto">{list}</aside>}
      <XtermSession key={`${workspace.id}:${me.data.person}:${s.id}:${interactive}`} addon={addon} session={s} interactive={interactive} fontSize={fontSize} rail={placement === 'rail'} cacheKey={`${workspace.id}:${me.data.person}:${addon}:${s.id}:${s.status}`} canCreate={can(role, 'addon.action')} action={action}
        picker={placement === 'page' && !wide ? <div className="relative shrink-0"><button aria-label="Choose terminal session" aria-expanded={picker} onClick={() => setPicker(!picker)} className="rounded px-1 hover:bg-surface-2">Sessions ▾</button>{picker && <div className="absolute left-0 top-7 z-20 max-h-80 w-[260px] overflow-auto rounded border border-border bg-surface p-1 shadow-lg">{list}</div>}</div> : undefined} />
    </div>
  </div>
}

function XtermSession({ addon, session, interactive, fontSize, rail, picker, cacheKey, canCreate, action }: {
  addon: string; session: TerminalSessionView; interactive: boolean; fontSize: number; rail: boolean; picker?: ReactNode; cacheKey: string; canCreate: boolean; action: (name: string, id?: string) => Promise<void>
}) {
  const qc = useQueryClient()
  const router = useRouter()
  const host = useRef<HTMLDivElement>(null)
  const leave = useRef<HTMLButtonElement>(null)
  const terminal = useRef<Terminal | null>(null)
  const search = useRef<SearchAddon | null>(null)
  const latest = useRef({ session, action })
  latest.current = { session, action }
  const [attached, setAttached] = useState(true)
  const [note, setNote] = useState(false)
  const [following, setFollowing] = useState(true)
  const [finding, setFinding] = useState(false)
  const [query, setQuery] = useState('')
  const [findResult, setFindResult] = useState('')
  const [feedback, setFeedback] = useState('')
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const followingRef = useRef(true)
  const output = useRef('')
  const preview = useRef('')
  const showNote = () => {
    setNote(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setNote(false), 4000)
  }
  useEffect(() => () => clearTimeout(timer.current), [])

  useEffect(() => {
    const el = host.current
    if (!el || !attached) return
    let disposed = false
    let term: Terminal | undefined
    let fit: FitAddon | undefined
    const id = Symbol(cacheKey)
    const subscriptions: { dispose: () => void }[] = []
    const cache = saved.get(qc) ?? new Map()
    saved.set(qc, cache)
    const previous = cache.get(cacheKey)
    output.current = previous?.output ?? (interactive ? createShell(() => latest.current.session.ctx).prompt() : replay(latest.current.session.ctx, latest.current.session.transcript) + (session.status === 'stopped' ? '[process completed]\r\n' : ''))
    preview.current = output.current.split(/\r?\n/).slice(-20).map(clean).join('\n')
    followingRef.current = previous?.following ?? true
    setFollowing(followingRef.current)
    const cleanup = () => {
      if (disposed) return
      disposed = true
      ro.disconnect() // stop fits before xterm tears down its renderer
      if (term) {
        cache.set(cacheKey, { output: output.current, viewport: term.buffer.active.viewportY, following: followingRef.current })
        // Bound retained transcripts to the most recent 40 sessions per query client.
        if (cache.size > 40) cache.delete(cache.keys().next().value!)
        preview.current = Array.from({ length: term.buffer.active.length }, (_, i) => term!.buffer.active.getLine(i)?.translateToString(true) ?? '').slice(-20).join('\n')
        subscriptions.forEach((s) => s.dispose())
        term.dispose()
        terminal.current = null
        search.current = null
      }
      live.delete(id)
    }
    const mountOrFit = () => {
      if (disposed || el.clientWidth === 0 || el.clientHeight === 0) return
      if (term) { fit?.fit(); return }
      while (live.size >= 2) live.values().next().value?.()
      live.set(id, () => { cleanup(); setAttached(false) })
      term = new Terminal({ fontSize, fontFamily: 'Geist Mono, ui-monospace, monospace', cursorBlink: interactive, disableStdin: !interactive, convertEol: false, linkHandler: NO_LINKS, theme: { background: token(el, '--bg'), foreground: token(el, '--text'), cursor: token(el, '--brand') } })
      terminal.current = term
      fit = new FitAddon()
      term.loadAddon(fit)
      search.current = new SearchAddon()
      term.loadAddon(search.current)
      term.open(el)
      fit.fit()
      term.textarea?.setAttribute('aria-label', `${session.label} input`)
      if (!interactive) term.textarea?.setAttribute('aria-readonly', 'true')
      let lastEscape = -Infinity
      term.attachCustomKeyEventHandler((e) => {
        if (e.type !== 'keydown') return true
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); setFinding(true); return false }
        if (!interactive) {
          if (e.key === 'Tab' || e.key === 'Escape') { e.preventDefault(); leave.current?.focus(); return false }
          if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) showNote()
          return false
        }
        if (e.key === 'Tab') e.preventDefault() // the shell gets Tab; the browser must not move focus
        if (e.key === 'Escape') {
          const now = performance.now()
          if (now - lastEscape <= 600) { e.preventDefault(); lastEscape = -Infinity; leave.current?.focus(); return false }
          lastEscape = now
        } else lastEscape = -Infinity
        return true
      })
      const write = (text: string) => {
        output.current += text
        const viewport = term!.buffer.active.viewportY
        term!.write(text, () => {
          if (disposed) return
          if (followingRef.current) term!.scrollToBottom()
          else term!.scrollToLine(viewport)
        })
      }
      term.write(output.current, () => {
        if (disposed) return
        if (previous && !previous.following) term!.scrollToLine(previous.viewport)
        else term!.scrollToBottom()
      })
      subscriptions.push(term.onScroll(() => {
        if (disposed) return
        followingRef.current = term!.buffer.active.viewportY >= term!.buffer.active.baseY
        setFollowing(followingRef.current)
      }))
      if (interactive) {
        const shell = createShell(() => latest.current.session.ctx)
        let closed = false
        subscriptions.push(term.onData((d) => {
          const out = shell.feed(d) // all data-derived output goes through fakePty.clean()
          if (out) write(out)
          if (shell.exited() && !closed) {
            closed = true
            write('[process completed]\r\n')
            void latest.current.action('close', session.id)
          }
        }))
      }
    }
    const ro = new ResizeObserver(mountOrFit)
    ro.observe(el)
    mountOrFit()
    return cleanup
  }, [cacheKey, attached, interactive, fontSize, qc, session.id, session.status])

  const transcript = () => {
    const term = terminal.current
    return term ? Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i)?.translateToString(true) ?? '').join('\n') : output.current.split(/\r?\n/).map(clean).join('\n')
  }
  const find = () => {
    if (!query) return
    setFindResult(search.current?.findNext(query, { caseSensitive: false }) ? 'Match found' : 'No matches')
  }
  return <section className="min-w-0 overflow-hidden rounded-md border border-border bg-bg" onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); setFinding(true) } }}>
    <TerminalHeader session={session} interactive={interactive} rail={rail} picker={picker} leaveRef={leave}
      onCopy={() => { void navigator.clipboard.writeText(terminal.current?.getSelection() || transcript()).then(() => setFeedback('Copied transcript'), () => setFeedback('Could not copy transcript')) }}
      onFind={() => setFinding(!finding)}
      onDownload={() => { const url = URL.createObjectURL(new Blob([transcript()], { type: 'text/plain' })); const a = document.createElement('a'); a.href = url; a.download = `${session.id}.txt`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 0) }}
      onEnd={() => void action('close', session.id)}
      onOpen={() => { void action('open', session.id).then(() => router.navigate({ to: '/addon/$name/$page', params: { name: addon, page: 'sessions' } })) }} />
    {finding && <form className="flex gap-2 p-2 text-xs" onSubmit={(e) => { e.preventDefault(); find() }}><input autoFocus aria-label="Find in terminal" value={query} onChange={(e) => setQuery(e.target.value)} className="min-w-0 rounded border border-border bg-surface px-2" /><button type="submit">Find next</button><button type="button" onClick={() => setFinding(false)}>Close find</button><span role="status">{findResult}</span></form>}
    {note && <div role="status" className="px-2 py-1 text-xs text-text-muted">Agent output is view only. Open your own shell to type. <button disabled={!canCreate} onClick={() => void action('new')} className="text-brand disabled:text-text-faint">New terminal</button></div>}
    {feedback && <p role="status" className="px-2 text-xs text-text-muted">{feedback}</p>}
    {attached ? <div ref={host} role="group" aria-label={`Terminal: ${session.label}`} aria-readonly={interactive ? undefined : true} data-terminal-session={session.id} data-terminal-rows={rail ? 12 : 24}
      style={{ height: rail ? `${Math.ceil(12 * fontSize * 1.25) + 16}px` : 'calc(100vh - 220px)', minHeight: rail ? undefined : 360 }} className="w-full overflow-hidden bg-bg p-2" />
      : <div className="p-2"><pre aria-label={`Last 20 lines: ${session.label}`} className="max-h-96 overflow-auto text-xs">{preview.current}</pre><button onClick={() => setAttached(true)}>Attach terminal</button></div>}
    {!following && <button className="m-2 rounded bg-brand px-2 py-1 text-xs text-on-brand" onClick={() => { followingRef.current = true; terminal.current?.scrollToBottom(); setFollowing(true) }}>Jump to latest</button>}
    {!interactive && <details className="px-2 text-xs text-text-muted"><summary>Plain-text transcript</summary><pre tabIndex={0} aria-label="Terminal transcript" className="max-h-64 overflow-auto whitespace-pre-wrap">{replay(session.ctx, session.transcript)}</pre></details>}
  </section>
}
