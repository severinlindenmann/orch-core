// Core-rendered terminal; the declarative node retains the pty capability gate.
import '@xterm/xterm/css/xterm.css'
import { FitAddon } from '@xterm/addon-fit'
import { SearchAddon } from '@xterm/addon-search'
import { Terminal } from '@xterm/xterm'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { ArrowDown, ChevronDown, X } from 'lucide-react'
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type Ref, type RefObject } from 'react'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { TerminalSessionView } from '@/api/terminals'
import { useAddonStates } from '@/addon-ui/slots'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { usePageWidth } from '../pageWidth'
import { useRole } from '../useRole'
import { useWorkspace } from '../workspace'
import { clean } from './fakePty'
import { screenDriver } from './harnessView'
import { SessionList } from './SessionList'
import { SessionStrip } from './SessionStrip'
import { TerminalHeader } from './TerminalHeader'

const token = (el: HTMLElement, name: string) => getComputedStyle(el).getPropertyValue(name).trim() || undefined
const NO_LINKS = { activate: () => {}, hover: () => {}, leave: () => {} }
// A global budget includes page and ticket-rail terminals. Eviction disposes synchronously before mounting another.
const live = new Map<symbol, () => void>()
const saved = new WeakMap<QueryClient, Map<string, { output: string; viewport: number; following: boolean }>>()

/** Height that fills the viewport from the element's top edge down (never below `min`); re-measured on every render and resize. */
function useFillHeight(ref: RefObject<HTMLElement | null>, enabled: boolean, min: number, bottom: number) {
  const [h, setH] = useState<number>()
  const measure = () => {
    const el = ref.current
    if (!enabled || !el) return
    const next = Math.max(min, Math.floor(window.innerHeight - el.getBoundingClientRect().top - bottom))
    setH((cur) => (cur === next ? cur : next))
  }
  useLayoutEffect(measure)
  useEffect(() => {
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  })
  return enabled ? h : undefined
}

/** `page`: the Terminals page (session list beside it); `rail`: a ticket panel; `dock`: the shell's terminal dock (fills its pane). */
/** The dock's extras: its name for the window, a footer above the strip, and where to put focus. */
export interface DockOptions {
  name: string
  /** A narrow dock: the strip leaves out what the tabs already say. */
  compact?: boolean
  footer?: ReactNode
  stripRef?: Ref<HTMLDivElement>
}

export default function TerminalView({ addon, session, fallback, placement = 'page', dock }: { addon: string; session: string; fallback: ReactNode; placement?: 'page' | 'rail' | 'dock'; dock?: DockOptions }) {
  const { workspace } = useWorkspace()
  const { [addon]: state } = useAddonStates(workspace?.id, [addon])
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const role = useRole()
  const qc = useQueryClient()
  const wide = usePageWidth() >= 1280
  const [picker, setPicker] = useState(false)
  const [error, setError] = useState('')
  const aside = useRef<HTMLElement>(null)
  const listHeight = useFillHeight(aside, placement === 'page' && wide, 200, 40)
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
  const xterm = (picker?: ReactNode) => <XtermSession key={`${workspace.id}:${me.data!.person}:${s.id}:${interactive}`} addon={addon} session={s} interactive={interactive} fontSize={fontSize} placement={placement} cacheKey={`${workspace.id}:${me.data!.person}:${addon}:${s.id}:${s.status}`} canCreate={can(role, 'addon.action')} action={action} picker={picker} dock={dock} />
  if (placement === 'dock') return <div className="flex h-full min-w-0 flex-col">
    {error && <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-1 text-xs text-danger">{error}</p>}
    <div className="min-h-0 flex-1">{xterm()}</div>
  </div>
  const list = <SessionList sessions={sessions} selected={s.id} onSelect={(id) => { void action('open', id); setPicker(false) }} />
  return <div className="min-w-0">
    {error && <p role="alert" className="mb-2 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger">{error}</p>}
    <div className={`grid min-w-0 gap-3 ${placement === 'page' && wide ? 'grid-cols-[260px_minmax(0,1fr)]' : 'grid-cols-1'}`}>
      {placement === 'page' && wide && <aside ref={aside} style={{ maxHeight: listHeight }} className="overflow-y-auto">{list}</aside>}
      {xterm(placement === 'page' && !wide ? <div className="relative shrink-0"><Button variant="ghost" size="xs" aria-label="Choose terminal session" aria-expanded={picker} onClick={() => setPicker(!picker)}>Sessions<ChevronDown /></Button>{picker && <div className="absolute left-0 top-7 z-20 max-h-80 w-[260px] overflow-auto rounded border border-border bg-surface p-1 shadow-lg">{list}</div>}</div> : undefined)}
    </div>
  </div>
}

function XtermSession({ addon, session, interactive, fontSize, placement, picker, cacheKey, canCreate, action, dock: dockOptions }: {
  addon: string; session: TerminalSessionView; interactive: boolean; fontSize: number; placement: 'page' | 'rail' | 'dock'; picker?: ReactNode; dock?: DockOptions; cacheKey: string; canCreate: boolean; action: (name: string, id?: string) => Promise<void>
}) {
  const rail = placement === 'rail'
  const dock = placement === 'dock' && !!dockOptions
  const qc = useQueryClient()
  const router = useRouter()
  const host = useRef<HTMLDivElement>(null)
  const leave = useRef<HTMLButtonElement>(null)
  const region = useRef<HTMLElement>(null)
  const helpId = useId()
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
  const [escHint, setEscHint] = useState(false)
  const escTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const followingRef = useRef(true)
  const output = useRef('')
  const preview = useRef('')
  const showNote = () => {
    setNote(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setNote(false), 4000)
  }
  useEffect(() => () => { clearTimeout(timer.current); clearTimeout(escTimer.current) }, [])

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
    // The harness decides the first screen, input, live line and redraws (harnessView.screenDriver); this effect owns
    // xterm's lifecycle. The first screen depends on the width (agent CLIs draw boxes), so it is drawn once xterm knows
    // its columns; `output` holds a 20-line preview until then.
    const cached = interactive ? previous?.output : undefined
    const driver = screenDriver(() => latest.current.session, interactive, () => term?.cols || 80)
    output.current = cached ?? ''
    let stopLive = () => {}
    preview.current = output.current.split(/\r?\n/).slice(-20).map(clean).join('\n')
    followingRef.current = previous?.following ?? true
    setFollowing(followingRef.current)
    const cleanup = () => {
      if (disposed) return
      disposed = true
      ro.disconnect() // stop fits before xterm tears down its renderer
      stopLive()
      if (term) {
        cache.set(cacheKey, { output: output.current, viewport: term.buffer.active.viewportY, following: followingRef.current })
        // Bound retained transcripts to the most recent 40 sessions per query client.
        if (cache.size > 40) cache.delete(cache.keys().next().value!)
        preview.current = Array.from({ length: term.buffer.active.length }, (_, i) => term!.buffer.active.getLine(i)?.translateToString(true) ?? '').slice(-20).join('\n')
        subscriptions.forEach((s) => s.dispose())
        // xterm schedules animation-frame work (viewport sync) when it opens; disposing before it runs throws
        // "reading 'dimensions'" (hit by StrictMode's mount/unmount/mount). Dispose after those frames have run.
        // Long screens (agent CLIs) are still being parsed and refresh the viewport on a timer: wait for the write
        // queue to drain and that timer to run first.
        const dying = term
        dying.write('', () => setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(() => dying.dispose())), 0))
        terminal.current = null
        search.current = null
      }
      live.delete(id)
    }
    const mountOrFit = () => {
      if (disposed || el.clientWidth === 0 || el.clientHeight === 0) return
      if (term) {
        const before = term.cols
        fit?.fit()
        if (term.cols !== before) {
          const again = driver.resized()
          if (again !== null) {
            term.reset()
            output.current = again
            term.write(again, () => { if (!disposed && followingRef.current) term!.scrollToBottom() })
          }
        }
        return
      }
      while (live.size >= 2) live.values().next().value?.()
      live.set(id, () => { cleanup(); setAttached(false) })
      term = new Terminal({
        fontSize, fontFamily: 'Geist Mono, ui-monospace, monospace', cursorBlink: interactive, disableStdin: !interactive, convertEol: false, linkHandler: NO_LINKS,
        // ANSI colours from the dashboard tokens (agent CLIs colour their marks); no orange.
        theme: { background: token(el, '--bg'), foreground: token(el, '--text'), cursor: token(el, '--brand'), green: token(el, '--success'), red: token(el, '--danger'), cyan: token(el, '--brand'), yellow: token(el, '--warning'), blue: token(el, '--info'), brightBlack: token(el, '--text-faint') },
      })
      terminal.current = term
      fit = new FitAddon()
      term.loadAddon(fit)
      search.current = new SearchAddon()
      term.loadAddon(search.current)
      term.open(el)
      fit.fit()
      // xterm sizes its viewport over the next frames: keep it hidden until the viewport has its size, so it appears
      // at its final size instead of growing in place (the host's background is the terminal's: nothing flashes).
      const drawn = term.element
      if (drawn) {
        drawn.style.visibility = 'hidden'
        let frames = 0
        let last = ''
        // Shown once the viewport sits inside the host at (about) its full height, the same for two frames in a row.
        const reveal = () => {
          if (disposed) return
          const v = drawn.querySelector<HTMLElement>('.xterm-viewport')?.getBoundingClientRect()
          const h = el.getBoundingClientRect()
          const now = v ? `${Math.round(v.top)}:${Math.round(v.height)}` : ''
          const settled = !!v && v.height >= h.height / 2 && v.top >= h.top - 1 && v.bottom <= h.bottom + 1 && now === last
          last = now
          if (settled || ++frames > 30) drawn.style.visibility = ''
          else requestAnimationFrame(reveal)
        }
        requestAnimationFrame(reveal)
      }
      if (cached === undefined) output.current = driver.first()
      term.textarea?.setAttribute('aria-label', `${session.label} input`)
      // The first time you are in your own terminal: say how to get out (Tab belongs to the shell here).
      if (interactive && term.textarea) {
        const ta = term.textarea
        const hint = () => { ta.removeEventListener('focus', hint); setEscHint(true); clearTimeout(escTimer.current); escTimer.current = setTimeout(() => setEscHint(false), 3000) }
        ta.addEventListener('focus', hint)
        subscriptions.push({ dispose: () => ta.removeEventListener('focus', hint) })
      }
      if (!interactive) term.textarea?.setAttribute('aria-readonly', 'true')
      let lastEscape = -Infinity
      term.attachCustomKeyEventHandler((e) => {
        if (e.type !== 'keydown') return true
        if (e.metaKey && e.key.toLowerCase() === 'f') { e.preventDefault(); setFinding(true); return false }
        if (!interactive) {
          if (e.key === 'Tab' || e.key === 'Escape') { e.preventDefault(); leave.current?.focus(); return false }
          if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) { showNote(); return false }
          return /^(Page(Up|Down)|Home|End|Arrow(Up|Down))$/.test(e.key) // reading keys scroll; stdin is disabled, so nothing is sent

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
      // A running agent CLI keeps its "working" line alive; the ticks are not cached output.
      stopLive = driver.live((text) => { if (!disposed) term!.write(text) })
      if (driver.feed) {
        const feed = driver.feed
        let closed = false
        subscriptions.push(term.onData((d) => {
          const out = feed(d) // all data-derived output goes through fakePty.clean()
          if (out) write(out)
          if (driver.exited() && !closed) {
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
  }, [cacheKey, attached, interactive, fontSize, qc, session.id, session.status, session.transcript.length])

  const transcript = () => {
    const term = terminal.current
    return term ? Array.from({ length: term.buffer.active.length }, (_, i) => term.buffer.active.getLine(i)?.translateToString(true) ?? '').join('\n') : output.current.split(/\r?\n/).map(clean).join('\n')
  }
  // Leave: hand focus to the next focusable thing after this terminal (or just drop it).
  const leaveTerminal = () => {
    const section = region.current
    if (!section) return
    const usable = (el: HTMLElement) => !section.contains(el) && !el.hidden && !el.closest('[hidden], [inert], [aria-hidden="true"]') && (el.getClientRects().length > 0 || import.meta.env.MODE === 'test')
    const all = Array.from(document.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    const next = all.find((el) => usable(el) && !!(section.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))
    if (next) next.focus()
    else (document.activeElement as HTMLElement | null)?.blur()
  }
  const find = () => {
    if (!query) return
    setFindResult(search.current?.findNext(query, { caseSensitive: false }) ? 'Match found' : 'No matches')
  }
  const copy = () => { void navigator.clipboard.writeText(terminal.current?.getSelection() || transcript()).then(() => setFeedback('Copied transcript'), () => setFeedback('Could not copy transcript')) }
  const download = () => { const url = URL.createObjectURL(new Blob([transcript()], { type: 'text/plain' })); const a = document.createElement('a'); a.href = url; a.download = `${session.id}.txt`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 0) }
  const fill = useFillHeight(host, placement === 'page' && attached, 360, 40)
  return <section ref={region} className={`min-w-0 overflow-hidden bg-bg ${dock ? 'flex h-full flex-col' : 'rounded-md border border-border'}`} onKeyDown={(e) => { if (e.metaKey && e.key.toLowerCase() === 'f' && attached) { e.preventDefault(); setFinding(true) } }}>
    {!dock && <TerminalHeader session={session} interactive={interactive} rail={rail} picker={picker} leaveRef={leave} canFind={attached} onLeave={leaveTerminal}
      onCopy={copy}
      onFind={() => setFinding(!finding)}
      onDownload={download}
      onEnd={() => void action('close', session.id)}
      onOpen={() => { void action('open', session.id).then(() => router.navigate({ to: '/addon/$name/$page', params: { name: addon, page: 'sessions' } })) }} />}
    {finding && attached && <form className="flex items-center gap-2 border-b border-border bg-surface px-2 py-1 text-xs" onSubmit={(e) => { e.preventDefault(); find() }}><Input autoFocus aria-label="Find in terminal" value={query} onChange={(e) => setQuery(e.target.value)} className="h-6 min-w-0 flex-1 px-2 text-xs md:text-xs" /><Button type="submit" variant="secondary" size="xs">Find next</Button><Button type="button" variant="ghost" size="icon-xs" aria-label="Close find" onClick={() => setFinding(false)}><X /></Button><span role="status" className="text-text-muted">{findResult}</span></form>}
    {note && <div role="status" className="px-2 py-1 text-xs text-text-muted">Agent output is view only. Open your own shell to type. <Button variant="link" size="xs" disabled={!canCreate} onClick={() => void action('new')}>New terminal</Button></div>}
    {feedback && <p role="status" className="px-2 text-xs text-text-muted">{feedback}</p>}
    {escHint && <p role="status" className="px-2 py-0.5 text-xs text-text-muted">Esc twice to leave the terminal</p>}
    {attached ? <div className={dock ? 'relative min-h-0 flex-1' : 'relative'}>
      <span id={helpId} className="sr-only">Terminal output is drawn on screen; use Download transcript for a text copy.</span>
      <div ref={host} role="group" aria-label={`Terminal: ${session.label}`} aria-describedby={helpId} aria-readonly={interactive ? undefined : true} data-terminal-session={session.id} data-terminal-rows={rail ? 12 : dock ? undefined : 24}
        style={{ height: rail ? `${Math.ceil(12 * fontSize * 1.25) + 16}px` : dock ? '100%' : fill ?? 'calc(100vh - 300px)', minHeight: rail || dock ? undefined : 360 }} className="w-full overflow-hidden bg-bg p-2" />
      {!following && <Button size="xs" className="absolute bottom-3 right-4 z-10" onClick={() => { followingRef.current = true; terminal.current?.scrollToBottom(); setFollowing(true) }}><ArrowDown />Jump to latest</Button>}
    </div>
      : <div className="p-2"><pre aria-label={`Last 20 lines: ${session.label}`} className="max-h-96 overflow-auto text-xs">{preview.current}</pre><Button variant="secondary" size="xs" className="mt-2" onClick={() => setAttached(true)}>Attach terminal</Button></div>}
    {dockOptions?.footer}
    {dockOptions && <SessionStrip session={session} interactive={interactive} name={dockOptions.name} compact={dockOptions.compact} stripRef={dockOptions.stripRef} leaveRef={leave} canFind={attached} onLeave={leaveTerminal}
      onCopy={copy} onFind={() => setFinding(!finding)} onDownload={download} onEnd={() => void action('close', session.id)} />}
  </section>
}
