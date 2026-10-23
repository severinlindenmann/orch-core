import { useEffect, useRef, useState, type ClipboardEvent } from 'react'
import { ArrowLeft, Check, Copy, Download, Plus, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { copyText } from '../copyLink'
import { buildReport, emptyState, flatten, hasOwnContent, imageName, isFilled, makeZip, newItem, normalize, numbered, zipFiles, type FeedbackItem, type FeedbackState } from './model'

const KEY = 'orch-feedback'
const DIR_KEY = 'orch-feedback-dir'
const IMAGE_MAX = 5 * 1024 * 1024

function load(): FeedbackState {
  try {
    const s = JSON.parse(localStorage.getItem(KEY) ?? 'null') as FeedbackState | null
    if (s && Array.isArray(s.items)) return { items: s.items, uid: s.uid ?? 0, round: s.round ?? null }
  } catch {
    /* no storage or a broken value: start empty */
  }
  return emptyState()
}
const toData = (f: File) =>
  new Promise<string>((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(f)
  })

function Field(props: { item: FeedbackItem; path: string; tail: boolean; onChange: (fn: (it: FeedbackItem) => void) => void; onDelete: () => void; onSub?: () => void; autoFocus: boolean }) {
  const { item, path, tail } = props
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [item.text])
  useEffect(() => {
    if (props.autoFocus) ref.current?.focus()
  }, [props.autoFocus])
  const onPaste = (e: ClipboardEvent) => {
    const files = Array.from(e.clipboardData.items).filter((x) => x.type.startsWith('image/')).map((x) => x.getAsFile()).filter((f): f is File => !!f && f.size <= IMAGE_MAX)
    if (files.length === 0) return
    e.preventDefault()
    void Promise.all(files.map(toData)).then((urls) => props.onChange((it) => void it.shots.push(...urls)))
  }
  return (
    <div className={cn('rounded-[10px] border p-3 focus-within:border-brand', tail ? 'border-dashed border-border bg-transparent' : 'border-border bg-surface')} data-testid={`fb-${path}`}>
      <div className="mb-1.5 flex items-center gap-2">
        <span className="font-mono text-xs font-semibold text-brand">Feedback {path}:</span>
        <span className="flex-1" />
        {props.onSub && !tail && <Button type="button" variant="ghost" size="xs" onClick={props.onSub}><Plus /> sub-feedback</Button>}
        {!tail && <Button type="button" variant="ghost" size="xs" aria-label={`Delete feedback ${path}`} onClick={props.onDelete}><Trash2 /> Delete</Button>}
      </div>
      <Textarea
        ref={ref}
        rows={1}
        value={item.text}
        aria-label={`Feedback ${path}`}
        placeholder={tail ? 'Next feedback… (paste a screenshot with Cmd+V)' : 'What did you notice?'}
        className="min-h-6 resize-none overflow-hidden border-0 bg-transparent p-0 shadow-none focus-visible:ring-0"
        onChange={(e) => props.onChange((it) => void (it.text = e.target.value))}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && props.onSub) props.onSub()
        }}
        onPaste={onPaste}
      />
      {item.shots.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-2">
          {item.shots.map((src, k) => (
            <li key={k} className="relative">
              <img src={src} alt={`Screenshot ${k + 1} of feedback ${path}`} className="max-h-32 max-w-56 rounded-md border border-border-strong" />
              <button type="button" aria-label={`Remove screenshot ${k + 1} of feedback ${path}`} className="absolute right-1 top-1 grid size-5 place-items-center rounded-full bg-black/70 text-white" onClick={() => props.onChange((it) => void it.shots.splice(k, 1))}>
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function FeedbackPage() {
  const [state, setState] = useState<FeedbackState>(() => normalize(load()))
  const [dir, setDir] = useState(() => {
    try {
      return localStorage.getItem(DIR_KEY) ?? ''
    } catch {
      return ''
    }
  })
  const [report, setReport] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const [focus, setFocus] = useState<number | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(state))
    } catch {
      /* storage full or blocked: the page still works for this visit */
    }
  }, [state])
  const say = (m: string) => {
    setNote(m)
    setTimeout(() => setNote(null), 2500)
  }
  // Every edit works on a copy, then keeps the one empty field at the end.
  const edit = (fn: (s: FeedbackState) => void) =>
    setState((prev) => {
      const next = structuredClone(prev)
      fn(next)
      return normalize(next)
    })
  const find = (s: FeedbackState, id: number): { list: FeedbackItem[]; i: number } | undefined => {
    const walk = (list: FeedbackItem[]): { list: FeedbackItem[]; i: number } | undefined => {
      for (let i = 0; i < list.length; i++) {
        if (list[i].id === id) return { list, i }
        const hit = walk(list[i].kids)
        if (hit) return hit
      }
    }
    return walk(s.items)
  }
  const change = (id: number, fn: (it: FeedbackItem) => void) => edit((s) => {
    const hit = find(s, id)
    if (hit) fn(hit.list[hit.i])
  })
  const remove = (id: number) => edit((s) => {
    const hit = find(s, id)
    if (hit) hit.list.splice(hit.i, 1)
  })
  const addSub = (id: number) => edit((s) => {
    const hit = find(s, id)
    if (!hit) return
    const parent = hit.list[hit.i]
    parent.kids = parent.kids.filter(isFilled)
    const kid = newItem(s)
    parent.kids.push(kid)
    setFocus(kid.id)
  })

  const folder = () => (dir.trim() || '~/Downloads').replace(/\/?$/, '/')
  const copyReport = async () => {
    const text = buildReport(state, folder(), false)
    if (!text) return say('Nothing to copy yet')
    setReport(text)
    try {
      localStorage.setItem(DIR_KEY, dir)
    } catch {
      /* not remembered */
    }
    say((await copyText(text)) ? 'Report copied' : 'Select the text below and copy it')
  }
  const download = (href: string, name: string) => {
    const a = Object.assign(document.createElement('a'), { href, download: name })
    a.click()
  }
  const exportZip = () => {
    if (!state.round || numbered(state.items).length === 0) return say('Nothing to export yet')
    const url = URL.createObjectURL(makeZip(zipFiles(state)))
    download(url, `feedback-${state.round}.zip`)
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
    say(`feedback-${state.round}.zip downloaded`)
  }
  const downloadImages = () => {
    for (const { path, item } of numbered(state.items)) item.shots.forEach((src, k) => download(src, imageName(state.round ?? '', path, k)))
  }
  const newRound = () => {
    if (state.items.some(isFilled) && !window.confirm(`Close round ${state.round} and start an empty one? Export or copy it first.`)) return
    setReport('')
    setState(normalize(emptyState()))
  }

  const all = flatten(state.items)
  const count = all.filter(hasOwnContent).length
  const shots = all.reduce((n, it) => n + it.shots.length, 0)
  const render = (list: FeedbackItem[], prefix: string) => {
    let n = 0
    return list.map((it, i) => {
      const tail = !isFilled(it) && i === list.length - 1
      if (!tail) n += 1
      const path = prefix ? `${prefix}.${tail ? n + 1 : n}` : `${tail ? n + 1 : n}`
      return (
        <li key={it.id} className="space-y-2">
          <Field item={it} path={path} tail={tail} autoFocus={focus === it.id} onChange={(fn) => change(it.id, fn)} onDelete={() => remove(it.id)} onSub={prefix ? undefined : () => addSub(it.id)} />
          {it.kids.length > 0 && <ul className="ml-5 space-y-2 border-l-2 border-border pl-3">{render(it.kids, path)}</ul>}
        </li>
      )
    })
  }

  return (
    <div className="h-full overflow-y-auto bg-bg">
      <div className="mx-auto max-w-3xl px-4 py-4 sm:py-6">
        {/* A full page load, like the checklist: this page sits outside the shell. */}
        <a href="/" className="inline-flex items-center gap-1.5 text-sm text-text-muted hover:text-text"><ArrowLeft className="size-4" /> Back to the dashboard</a>
        <h1 className="mt-3 text-xl font-semibold text-text">Feedback</h1>
        <p className="mt-1 text-sm text-text-muted">Write what you noticed and paste screenshots with Cmd+V. The next field opens by itself. Add sub-points (1.1, 1.2) with “sub-feedback”, then copy the report or export a ZIP for the agent. Saved in this browser only.</p>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-text-muted" data-testid="fb-round">{state.round ? `round ${state.round}` : 'no round yet'}</span>
          <span className="rounded-full bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-text-muted" data-testid="fb-count">{count} item{count === 1 ? '' : 's'} · {shots} screenshot{shots === 1 ? '' : 's'}</span>
          <span className="flex-1" />
          <Button type="button" variant="outline" size="sm" onClick={newRound}>Start new round</Button>
          <Button type="button" variant="outline" size="sm" onClick={exportZip}><Download /> Export ZIP</Button>
          <Button type="button" size="sm" onClick={copyReport}><Copy /> Copy report</Button>
        </div>
        {note && <p role="status" className="mt-2 flex items-center gap-1.5 text-sm text-brand"><Check className="size-4" /> {note}</p>}

        <ul className="mt-4 space-y-2">{render(state.items, '')}</ul>

        {report && (
          <section className="mt-6" aria-label="Report preview">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <b className="text-sm text-text">Report preview</b>
              <span className="flex-1" />
              <label className="flex items-center gap-1.5 text-xs text-text-muted">
                Images saved in
                <input value={dir} onChange={(e) => setDir(e.target.value)} placeholder="~/Downloads" className="w-48 rounded-md border border-border bg-surface px-2 py-1 text-text" />
              </label>
              <Button type="button" variant="outline" size="xs" onClick={downloadImages}><Download /> Download images</Button>
              <Button type="button" variant="outline" size="xs" onClick={copyReport}><Copy /> Copy again</Button>
            </div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-[10px] border border-border bg-surface p-3 font-mono text-xs text-text">{report}</pre>
          </section>
        )}
      </div>
    </div>
  )
}
