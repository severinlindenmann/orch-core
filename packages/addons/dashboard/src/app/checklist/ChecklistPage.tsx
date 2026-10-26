import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import { ArrowLeft, Check, ClipboardCopy, Copy, Download, ImagePlus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import * as testrun from '@/api/testrun'
import { addImage, imageProblem, imageUrl, loadPrevious, loadRun, removeImage, saveStep, STATUSES, type PreviousRun, type StepNotes, type StepStatus, TestrunError, retryable } from '@/api/testrun'
import { cn } from '@/lib/utils'
import { copyText } from '../copyLink'
import { ALL_STEPS, RUN2_INTRO, RUN2_KNOWN_GAPS, RUN2_REPORT_HELP, RUN2_SECTIONS, RUN2_SUBTITLE, RUN2_TITLE, RUN_ID, RUN_NUMBER, type ChecklistStep } from './run2'
import { carryFor, dayText, runLabel, stepHash, type Carry } from './carry'
import { buildReport, emptyNotes, hasNoteOrFailure, isFailure, STATUS_LABEL } from './report'

const SAVE_DELAY = 700
const RETRIES = 8
const RETRY_BASE = 500

type Saving = 'saving' | 'saved' | 'error'

const statusTone: Record<StepStatus, string> = {
  todo: 'border-border text-text-muted',
  pass: 'border-success bg-success-soft text-success',
  fail_blocking: 'border-danger bg-danger-soft text-danger',
  fail_non_blocking: 'border-warning bg-warning-soft text-warning',
  skipped: 'border-border-strong bg-surface-3 text-text',
}

function problemText(e: unknown): string {
  if (e instanceof TestrunError && e.status >= 400 && e.status < 500 && e.status !== 401 && e.status !== 408 && e.status !== 429) return 'The dashboard did not accept this change, so it was not saved. Reload the page and try again.'
  if (e instanceof TestrunError && e.status === 401) return 'Your dashboard session ended. Run "orch dashboard open /checklist" in the Terminal, then come back. Nothing you typed here is lost until you reload.'
  return 'Could not reach the dashboard server. Your last change may not be saved.'
}

function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button
      type="button"
      variant="outline"
      size="xs"
      aria-label={`${label}: ${text.length > 60 ? text.slice(0, 57) + '...' : text}`}
      onClick={async () => {
        if (await copyText(text)) {
          setDone(true)
          setTimeout(() => setDone(false), 1500)
        }
      }}
    >
      {done ? <Check /> : <Copy />}
      {done ? 'Copied' : label}
    </Button>
  )
}

function Shot({ run, name, path, onRemove }: { run: string; name: string; path: string; onRemove: () => void }) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    imageUrl(run, name).then((u) => live && setSrc(u), () => undefined)
    return () => {
      live = false
    }
  }, [run, name])
  return (
    <li className="w-40 min-w-0 rounded-md border border-border bg-surface-2 p-1.5">
      {src ? <img src={src} alt={`Screenshot ${name}`} className="h-24 w-full rounded object-cover" /> : <div className="h-24 w-full rounded bg-surface-3" />}
      <div className="mt-1 break-all text-[10px] leading-tight text-text-faint">{path}</div>
      <Button type="button" variant="ghost" size="xs" className="mt-1 w-full" onClick={onRemove} aria-label={`Remove screenshot ${name}`}>
        <Trash2 /> Remove
      </Button>
    </li>
  )
}

function StepCard(props: {
  step: ChecklistStep
  notes: StepNotes
  imagesPath: string
  saving?: Saving
  message?: string
  onChange: (patch: Partial<Pick<StepNotes, 'status' | 'note'>>, now?: boolean) => void
  onFlush: () => void
  onFiles: (files: File[]) => void
  onRemove: (name: string) => void
  carry?: Carry | null
  onRetest: () => void
}) {
  const { step, notes, imagesPath, carry } = props
  const file = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const pick = (list: FileList | File[] | null | undefined) => {
    const files = Array.from(list ?? []).filter((f) => f.type.startsWith('image/'))
    if (files.length) props.onFiles(files)
  }
  const onPaste = (e: ClipboardEvent) => {
    const files = Array.from(e.clipboardData?.files ?? [])
    if (files.some((f) => f.type.startsWith('image/'))) {
      e.preventDefault()
      pick(files)
    }
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setOver(false)
    pick(e.dataTransfer?.files)
  }
  if (carry?.kind === 'done') {
    const when = dayText(carry.date)
    return (
      <section id={`step-${step.id}`} aria-labelledby={`h-${step.id}`} className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[10px] border border-success/40 bg-success-soft px-3 py-2">
        <Check className="size-4 text-success" aria-hidden />
        <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-xs text-text">{step.id}</span>
        <h3 id={`h-${step.id}`} className="text-sm font-medium text-text">{step.title}</h3>
        <span className="text-xs text-success">Done in {runLabel(carry.run)}{when ? ` (${when})` : ''}</span>
        <Button type="button" variant="outline" size="xs" className="ml-auto" aria-label={`Test again: ${step.id} ${step.title}`} onClick={props.onRetest}>Test again</Button>
      </section>
    )
  }
  return (
    <section id={`step-${step.id}`} aria-labelledby={`h-${step.id}`} className={cn('rounded-[10px] border bg-surface p-3 sm:p-4', isFailure(notes) ? 'border-danger/60' : 'border-border')}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-xs text-text">{step.id}</span>
        <h3 id={`h-${step.id}`} className="text-[15px] font-medium text-text">{step.title}</h3>
        {step.optional && <span className="text-xs text-text-muted">(optional)</span>}
      </div>
      {carry?.kind === 'changed' && (
        <p role="note" className="mt-2 rounded-md bg-warning-soft px-2.5 py-1.5 text-sm text-text"><span className="font-medium text-warning">Changed since {runLabel(carry.run)}, please test again. </span>You passed an earlier version of this step.</p>
      )}
      {carry?.kind === 'context' && (
        <p role="note" className="mt-2 rounded-md bg-surface-2 px-2.5 py-1.5 text-sm text-text"><span className="font-medium text-text-muted">In {runLabel(carry.run)}: {STATUS_LABEL[carry.status]}. </span>{carry.note.trim() ? <span className="whitespace-pre-wrap break-words">{carry.note.trim()}</span> : 'No note.'}</p>
      )}
      {step.text && <p className="mt-2 text-sm text-text">{step.text}</p>}
      {step.points && (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text">
          {step.points.map((p) => <li key={p}>{p}</li>)}
        </ul>
      )}
      {step.commands && (
        <ul className="mt-2 space-y-1.5">
          {step.commands.map((c) => (
            <li key={c} className="flex flex-wrap items-start gap-2 rounded-md bg-surface-2 p-2">
              <code className="min-w-0 flex-1 break-all font-mono text-xs text-text">{c}</code>
              <CopyButton text={c} />
            </li>
          ))}
        </ul>
      )}
      {step.see && (
        <p className="mt-2 text-sm text-text"><span className="font-medium text-brand">You should see: </span>{step.see}</p>
      )}
      {step.warn && (
        <p className="mt-2 rounded-md bg-warning-soft px-2.5 py-1.5 text-sm text-text"><span className="font-medium text-warning">Note: </span>{step.warn}</p>
      )}

      <div role="radiogroup" aria-label={`Result of ${step.id}`} className="mt-3 flex flex-wrap gap-1.5">
        {STATUSES.map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={notes.status === s}
            onClick={() => props.onChange({ status: notes.status === s ? 'todo' : s }, true)}
            className={cn(
              'min-h-8 rounded-md border px-2.5 text-xs font-medium outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50',
              notes.status === s ? statusTone[s] : 'border-border bg-transparent text-text-muted hover:bg-surface-2',
            )}
          >
            {STATUS_LABEL[s]}
          </button>
        ))}
      </div>

      <Textarea
        aria-label={`Notes for ${step.id}`}
        placeholder="Notes: what you saw, what was confusing. Paste a screenshot here too."
        className="mt-2 min-h-20 text-sm"
        value={notes.note}
        maxLength={20000}
        onChange={(e) => props.onChange({ note: e.target.value })}
        onBlur={props.onFlush}
        onPaste={onPaste}
      />

      <div
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        className={cn('mt-2 flex flex-wrap items-center gap-2 rounded-md border border-dashed p-2 text-xs text-text-muted', over ? 'border-brand bg-brand-soft' : 'border-border-strong')}
      >
        <Button type="button" variant="outline" size="xs" onClick={() => file.current?.click()}><ImagePlus /> Add screenshot</Button>
        <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" multiple hidden aria-label={`Add screenshot to ${step.id}`} onChange={(e) => { pick(e.target.files); e.target.value = '' }} />
        <span>or paste or drop a picture (PNG, JPEG, WebP, up to 5 MB). Never a QR, link, code, word or passphrase.</span>
      </div>
      {notes.images.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-2" aria-label={`Screenshots of ${step.id}`}>
          {notes.images.map((name) => <Shot key={name} run={RUN_ID} name={name} path={`${imagesPath}/${name}`} onRemove={() => props.onRemove(name)} />)}
        </ul>
      )}
      <div className="mt-1.5 min-h-4 text-xs" aria-live="polite">
        {props.message ? <span className="text-danger">{props.message}</span> : props.saving === 'saving' ? <span className="text-text-faint">Saving...</span> : props.saving === 'saved' ? <span className="text-text-faint">Saved</span> : props.saving === 'error' ? <span className="text-danger">Not saved</span> : null}
      </div>
    </section>
  )
}

export function ChecklistPage() {
  const [notes, setNotes] = useState<Record<string, StepNotes>>({})
  const [paths, setPaths] = useState<{ path: string; images: string } | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [saving, setSaving] = useState<Record<string, Saving>>({})
  const [messages, setMessages] = useState<Record<string, string>>({})
  const [previous, setPrevious] = useState<PreviousRun[]>([])
  const [onlyNotes, setOnlyNotes] = useState(false)
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null)
  const [zipping, setZipping] = useState<'busy' | 'ok' | string | null>(null)
  const latest = useRef(notes)
  latest.current = notes
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  // The fields of each step changed here and not yet saved: only these are sent, and a late first load never replaces them.
  const dirty = useRef(new Map<string, Set<'status' | 'note'>>())
  // Every field edited on this page, kept after it was saved: a first load that answers late must not replace them.
  const touched = useRef(new Map<string, Set<'status' | 'note'>>())
  const chain = useRef<Promise<void>>(Promise.resolve())
  const attempts = useRef(new Map<string, number>())
  // Set once the page is gone: a save that fails then is not scheduled again (no timer outlives the page).
  const gone = useRef(false)

  useEffect(() => {
    let live = true
    loadRun(RUN_ID).then(
      (s) => {
        if (!live) return
        // Edits made before the first load finished stay on top of what the server held.
        setNotes((local) => {
          const merged = { ...s.notes }
          for (const [id, fields] of touched.current) {
            const mine = local[id] ?? emptyNotes()
            const base = merged[id] ?? emptyNotes()
            merged[id] = { ...base, ...(fields.has('status') ? { status: mine.status } : {}), ...(fields.has('note') ? { note: mine.note } : {}), images: [...new Set([...base.images, ...mine.images])] }
          }
          return merged
        })
        setPaths({ path: s.path, images: s.images_path })
        setReady(true)
        // Passes saved before the step fingerprint existed get it now, so a later run can tell if the step changed.
        for (const step of ALL_STEPS) {
          const n = s.notes[step.id]
          if (n?.status === 'pass' && !n.hash) void saveStep(RUN_ID, step.id, { hash: stepHash(step) }).catch(() => undefined)
        }
      },
      (e) => {
        if (!live) return
        setProblem(problemText(e))
        setReady(true)
      },
    )
    loadPrevious(RUN_ID).then((p) => live && setPrevious(p), () => undefined)
    return () => { live = false }
  }, [])

  const mark = (id: string, s: Saving) => setSaving((m) => ({ ...m, [id]: s }))

  const flush = useCallback(function flush(id: string): void {
    const t = timers.current.get(id)
    if (t) clearTimeout(t)
    timers.current.delete(id)
    const fields = dirty.current.get(id)
    if (!fields) return
    dirty.current.delete(id)
    mark(id, 'saving')
    // One request at a time: a later save of a step never overtakes an earlier one.
    chain.current = chain.current.then(async () => {
      const n = latest.current[id] ?? emptyNotes()
      const patch: { status?: StepStatus; note?: string; hash?: string } = {}
      if (fields.has('status')) {
        patch.status = n.status
        const step = ALL_STEPS.find((x) => x.id === id)
        if (n.status === 'pass' && step) patch.hash = stepHash(step)
      }
      if (fields.has('note')) patch.note = n.note
      try {
        await saveStep(RUN_ID, id, patch)
        attempts.current.delete(id)
        setProblem(null)
        mark(id, dirty.current.has(id) ? 'saving' : 'saved')
      } catch (e) {
        // A failed save is never dropped: the fields stay unsaved and go again (a stale keep-alive connection, a
        // restarting server). Only an ended session waits for the person.
        // A refusal that sending again cannot change (400, 403, 404, 422) is not kept: it would go out again with
        // every later edit of the step. An ended session (401) is kept for after the person signs in again.
        const refused = e instanceof TestrunError && !retryable(e) && e.status !== 401
        if (!refused) {
          const again = dirty.current.get(id) ?? new Set<'status' | 'note'>()
          for (const k of fields) again.add(k)
          dirty.current.set(id, again)
        }
        setProblem(problemText(e))
        mark(id, 'error')
        const tries = attempts.current.get(id) ?? 0
        const ended = e instanceof TestrunError && e.status === 401
        if (!ended && !gone.current && retryable(e) && tries < RETRIES && !timers.current.has(id)) {
          attempts.current.set(id, tries + 1)
          timers.current.set(id, setTimeout(() => flush(id), Math.min(RETRY_BASE * 2 ** tries, 8000)))
        }
      }
    })
  }, [])

  useEffect(() => {
    const out = timers.current
    gone.current = false
    const leave = () => { for (const id of [...dirty.current.keys()]) flush(id) }
    window.addEventListener('pagehide', leave)
    return () => {
      window.removeEventListener('pagehide', leave)
      for (const id of [...out.keys()]) flush(id)
      gone.current = true
    }
  }, [flush])

  const change = useCallback((id: string, patch: Partial<Pick<StepNotes, 'status' | 'note'>>, now = false) => {
    setNotes((m) => ({ ...m, [id]: { ...(m[id] ?? emptyNotes()), ...patch } }))
    latest.current = { ...latest.current, [id]: { ...(latest.current[id] ?? emptyNotes()), ...patch } }
    const fields = dirty.current.get(id) ?? new Set<'status' | 'note'>()
    for (const k of Object.keys(patch) as ('status' | 'note')[]) fields.add(k)
    dirty.current.set(id, fields)
    const seen = touched.current.get(id) ?? new Set<'status' | 'note'>()
    for (const k of fields) seen.add(k)
    touched.current.set(id, seen)
    const old = timers.current.get(id)
    if (old) clearTimeout(old)
    if (now) flush(id)
    else timers.current.set(id, setTimeout(() => flush(id), SAVE_DELAY))
  }, [flush])

  const upload = async (id: string, files: File[]) => {
    for (const file of files) {
      const why = imageProblem(file)
      if (why) { setMessages((m) => ({ ...m, [id]: why })); continue }
      setMessages((m) => ({ ...m, [id]: '' }))
      try {
        const out = await addImage(RUN_ID, id, file)
        setNotes((m) => ({ ...m, [id]: { ...(m[id] ?? emptyNotes()), images: [...(m[id]?.images ?? []), out.name] } }))
      } catch (e) {
        setMessages((m) => ({ ...m, [id]: e instanceof TestrunError && e.status === 400 ? 'The picture was not accepted (too many pictures on this step, or not a real PNG, JPEG or WebP).' : problemText(e) }))
      }
    }
  }
  const remove = async (id: string, name: string) => {
    try {
      await removeImage(RUN_ID, name)
      setNotes((m) => ({ ...m, [id]: { ...m[id], images: m[id].images.filter((x) => x !== name) } }))
    } catch (e) {
      setMessages((m) => ({ ...m, [id]: problemText(e) }))
    }
  }

  const get = (id: string) => notes[id] ?? emptyNotes()
  const carries = useMemo(() => {
    const out: Record<string, Carry> = {}
    for (const s of ALL_STEPS) {
      const c = carryFor(s, notes[s.id], previous)
      if (c) out[s.id] = c
    }
    return out
  }, [notes, previous])
  const retest = async (id: string) => {
    setNotes((m) => ({ ...m, [id]: { ...(m[id] ?? emptyNotes()), retest: true } }))
    try {
      await saveStep(RUN_ID, id, { retest: true })
    } catch (e) {
      setNotes((m) => ({ ...m, [id]: { ...(m[id] ?? emptyNotes()), retest: false } }))
      setMessages((m) => ({ ...m, [id]: problemText(e) }))
    }
  }
  const counts = useMemo(() => {
    const c: Record<StepStatus, number> = { todo: 0, pass: 0, fail_blocking: 0, fail_non_blocking: 0, skipped: 0 }
    for (const s of ALL_STEPS) c[carries[s.id]?.kind === 'done' ? 'pass' : get(s.id).status]++
    return c
  }, [notes, carries])
  const carriedDone = Object.values(carries).filter((c) => c.kind === 'done').length
  const imagesPath = paths?.images ?? ''
  const copyReport = async () => {
    setCopied((await copyText(buildReport(notes, imagesPath, paths?.path ?? '(unknown)', carries))) ? 'ok' : 'fail')
    setTimeout(() => setCopied(null), 2500)
  }

  const shotCount = Object.values(notes).reduce((n, r) => n + (r.images?.length ?? 0), 0)
  const downloadZip = async () => {
    setZipping('busy')
    try {
      await testrun.downloadScreenshots(RUN_ID)
      setZipping('ok')
    } catch (e) {
      setZipping(e instanceof testrun.TestrunError && e.code === 'zip.too_large' ? 'The screenshots are larger than 200 MB. Copy them from the folder shown above instead.' : e instanceof testrun.TestrunError && e.status === 404 ? 'There are no screenshots to download.' : 'Could not download the screenshots. Try again.')
    }
    setTimeout(() => setZipping(null), 4000)
  }

  return (
    <div className="h-full overflow-y-auto bg-bg">
      <div className="mx-auto max-w-3xl px-4 py-4 sm:py-6">
        {/* A full page load: the checklist opens without the session, so the dashboard has to ask for it again. */}
        <a href="/" className="inline-flex items-center gap-1.5 text-sm text-text-muted hover:text-text"><ArrowLeft className="size-4" /> Back to the dashboard</a>
        <h1 className="mt-3 text-xl font-semibold text-text">{RUN2_TITLE}</h1>
        <p className="mt-1 text-sm text-text-muted">{RUN2_SUBTITLE}</p>

        <div className="mt-3 rounded-[10px] border border-border bg-surface p-3 text-sm">
          {paths ? (
            <>
              <p className="text-text">Your notes are saved in:</p>
              <div className="mt-1 flex flex-wrap items-start gap-2">
                <code className="min-w-0 flex-1 break-all font-mono text-xs text-text">{paths.path}</code>
                <CopyButton text={paths.path} />
              </div>
              <p className="mt-1 text-xs text-text-muted">Only on this computer. The lead can read them from there; you can also use "Copy report" below.</p>
            </>
          ) : ready ? (
            <p className="text-text">Saving needs the real dashboard, opened from the Terminal with <code className="font-mono text-xs">orch dashboard open /checklist</code>.</p>
          ) : (
            <p className="text-text-muted">Loading your notes...</p>
          )}
          {problem && <p role="alert" className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-danger">{problem}</p>}
        </div>

        <div className="mt-3 space-y-1.5 text-sm text-text">
          {RUN2_INTRO.map((t) => <p key={t}>{t}</p>)}
        </div>

        <section aria-labelledby="gaps" className="mt-3 rounded-[10px] border border-warning/50 bg-warning-soft p-3">
          <h2 id="gaps" className="text-sm font-semibold text-text">Known gaps in run {RUN_NUMBER} (expected, do not report as bugs)</h2>
          <ul className="mt-1.5 list-disc space-y-1 pl-5 text-sm text-text">
            {RUN2_KNOWN_GAPS.map((g) => <li key={g}>{g}</li>)}
          </ul>
        </section>

        <div className="sticky top-0 z-10 -mx-4 mt-4 border-b border-border bg-bg/95 px-4 py-2 backdrop-blur">
          <nav aria-label="Sections" className="flex flex-wrap items-center gap-1.5">
            {RUN2_SECTIONS.map((s) => (
              <a key={s.id} href={`#section-${s.id}`} onClick={(e) => { e.preventDefault(); document.getElementById(`section-${s.id}`)?.scrollIntoView?.({ block: 'start' }) }} className="rounded-md border border-border px-2.5 py-1 text-xs font-medium text-text hover:bg-surface-2">
                {s.id} <span className="text-text-muted">{s.title}</span>
              </a>
            ))}
          </nav>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-text-muted">
            <span>{counts.pass} pass{carriedDone > 0 ? ` (${carriedDone} done earlier)` : ''}, {counts.fail_blocking + counts.fail_non_blocking} failed, {counts.skipped} skipped, {counts.todo} not yet</span>
            <label className="flex items-center gap-1.5 text-text">
              <input type="checkbox" checked={onlyNotes} onChange={(e) => setOnlyNotes(e.target.checked)} /> Only steps with notes or failures
            </label>
            <Button type="button" size="xs" onClick={copyReport}><ClipboardCopy /> Copy report</Button>
            <span role="status">{copied === 'ok' ? 'Report copied' : copied === 'fail' ? 'Could not copy' : ''}</span>
            {paths && shotCount > 0 && (
              <Button type="button" size="xs" variant="outline" disabled={zipping === 'busy'} onClick={downloadZip}>
                <Download /> {shotCount === 1 ? 'Download 1 screenshot (zip)' : `Download ${shotCount} screenshots (zip)`}
              </Button>
            )}
            <span role="status">{zipping === 'busy' ? 'Preparing the zip...' : zipping === 'ok' ? 'Zip downloaded' : zipping ?? ''}</span>
          </div>
        </div>

        {RUN2_SECTIONS.map((section) => {
          const steps = section.steps.filter((s) => !onlyNotes || hasNoteOrFailure(notes[s.id]))
          if (steps.length === 0) return null
          return (
            <section key={section.id} id={`section-${section.id}`} aria-labelledby={`sec-${section.id}`} className="mt-6 scroll-mt-28">
              <h2 id={`sec-${section.id}`} className="text-lg font-semibold text-text">{section.id}) {section.title}{section.where && <span className="ml-2 text-sm font-normal text-text-muted">{section.where}</span>}</h2>
              {section.intro && <p className="mt-1.5 text-sm text-text">{section.intro}</p>}
              {section.introCommands && (
                <ul className="mt-2 space-y-1.5">
                  {section.introCommands.map((c) => (
                    <li key={c} className="flex flex-wrap items-start gap-2 rounded-md bg-surface-2 p-2">
                      <code className="min-w-0 flex-1 break-all font-mono text-xs text-text">{c}</code>
                      <CopyButton text={c} />
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 space-y-3">
                {steps.map((step) => (
                  <div key={step.id}>
                    {step.group && !onlyNotes && <h3 className="mb-2 mt-4 text-sm font-semibold text-text-muted">{step.group}</h3>}
                    <StepCard
                      step={step}
                      notes={get(step.id)}
                      imagesPath={imagesPath}
                      saving={saving[step.id]}
                      message={messages[step.id]}
                      onChange={(p, now) => change(step.id, p, now)}
                      onFlush={() => flush(step.id)}
                      onFiles={(files) => void upload(step.id, files)}
                      onRemove={(name) => void remove(step.id, name)}
                      carry={carries[step.id]}
                      onRetest={() => void retest(step.id)}
                    />
                  </div>
                ))}
              </div>
            </section>
          )
        })}
        {onlyNotes && ALL_STEPS.every((s) => !hasNoteOrFailure(notes[s.id])) && <p className="mt-6 text-sm text-text-muted">No step has a note, a picture or a failure yet.</p>}

        <section id="section-G" aria-labelledby="sec-G" className="mt-6 scroll-mt-28">
          <h2 id="sec-G" className="text-lg font-semibold text-text">G) What to report back</h2>
          <p className="mt-1.5 text-sm text-text">{RUN2_REPORT_HELP.text}</p>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text">{RUN2_REPORT_HELP.points.map((p) => <li key={p}>{p}</li>)}</ul>
          <p className="mt-2 text-sm text-text">{RUN2_REPORT_HELP.cleanupText}</p>
          <ul className="mt-2 space-y-1.5">
            {RUN2_REPORT_HELP.cleanup.map((c) => (
              <li key={c} className="flex flex-wrap items-start gap-2 rounded-md bg-surface-2 p-2"><code className="min-w-0 flex-1 break-all font-mono text-xs text-text">{c}</code><CopyButton text={c} /></li>
            ))}
          </ul>
        </section>

      </div>
    </div>
  )
}
