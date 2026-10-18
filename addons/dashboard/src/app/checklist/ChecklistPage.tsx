import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import { Link } from '@tanstack/react-router'
import { ArrowLeft, Check, ClipboardCopy, Copy, ImagePlus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { addImage, imageProblem, imageUrl, loadRun, removeImage, saveStep, STATUSES, type StepNotes, type StepStatus, TestrunError } from '@/api/testrun'
import { cn } from '@/lib/utils'
import { copyText } from '../copyLink'
import { ALL_STEPS, INSTALL_PIN, RUN2_INTRO, RUN2_KNOWN_GAPS, RUN2_REPORT_HELP, RUN2_SECTIONS, RUN2_SUBTITLE, RUN2_TITLE, RUN_ID, type ChecklistStep } from './run2'
import { buildReport, emptyNotes, hasNoteOrFailure, isFailure, STATUS_LABEL } from './report'

const SAVE_DELAY = 700

type Saving = 'saving' | 'saved' | 'error'

const statusTone: Record<StepStatus, string> = {
  todo: 'border-border text-text-muted',
  pass: 'border-success bg-success-soft text-success',
  fail_blocking: 'border-danger bg-danger-soft text-danger',
  fail_non_blocking: 'border-warning bg-warning-soft text-warning',
  skipped: 'border-border-strong bg-surface-3 text-text',
}

function problemText(e: unknown): string {
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
}) {
  const { step, notes, imagesPath } = props
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
  return (
    <section id={`step-${step.id}`} aria-labelledby={`h-${step.id}`} className={cn('rounded-[10px] border bg-surface p-3 sm:p-4', isFailure(notes) ? 'border-danger/60' : 'border-border')}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-xs text-text">{step.id}</span>
        <h3 id={`h-${step.id}`} className="text-[15px] font-medium text-text">{step.title}</h3>
        {step.optional && <span className="text-xs text-text-muted">(optional)</span>}
      </div>
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
  const [onlyNotes, setOnlyNotes] = useState(false)
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null)
  const latest = useRef(notes)
  latest.current = notes
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const dirty = useRef(new Set<string>())
  const chain = useRef<Promise<void>>(Promise.resolve())

  useEffect(() => {
    let live = true
    loadRun(RUN_ID).then(
      (s) => {
        if (!live) return
        setNotes(s.notes)
        setPaths({ path: s.path, images: s.images_path })
        setReady(true)
      },
      (e) => {
        if (!live) return
        setProblem(problemText(e))
        setReady(true)
      },
    )
    return () => { live = false }
  }, [])

  const mark = (id: string, s: Saving) => setSaving((m) => ({ ...m, [id]: s }))

  const flush = useCallback((id: string) => {
    const t = timers.current.get(id)
    if (t) clearTimeout(t)
    timers.current.delete(id)
    if (!dirty.current.has(id)) return
    dirty.current.delete(id)
    mark(id, 'saving')
    // One request at a time: a later save of a step never overtakes an earlier one.
    chain.current = chain.current.then(async () => {
      const n = latest.current[id] ?? emptyNotes()
      try {
        await saveStep(RUN_ID, id, n.status, n.note)
        setProblem(null)
        mark(id, dirty.current.has(id) ? 'saving' : 'saved')
      } catch (e) {
        setProblem(problemText(e))
        mark(id, 'error')
      }
    })
  }, [])

  useEffect(() => {
    const out = timers.current
    const leave = () => { for (const id of [...dirty.current]) flush(id) }
    window.addEventListener('pagehide', leave)
    return () => {
      window.removeEventListener('pagehide', leave)
      for (const id of [...out.keys()]) flush(id)
    }
  }, [flush])

  const change = useCallback((id: string, patch: Partial<Pick<StepNotes, 'status' | 'note'>>, now = false) => {
    setNotes((m) => ({ ...m, [id]: { ...(m[id] ?? emptyNotes()), ...patch } }))
    latest.current = { ...latest.current, [id]: { ...(latest.current[id] ?? emptyNotes()), ...patch } }
    dirty.current.add(id)
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
  const counts = useMemo(() => {
    const c: Record<StepStatus, number> = { todo: 0, pass: 0, fail_blocking: 0, fail_non_blocking: 0, skipped: 0 }
    for (const s of ALL_STEPS) c[get(s.id).status]++
    return c
  }, [notes])
  const imagesPath = paths?.images ?? ''
  const copyReport = async () => {
    setCopied((await copyText(buildReport(notes, imagesPath, paths?.path ?? '(unknown)'))) ? 'ok' : 'fail')
    setTimeout(() => setCopied(null), 2500)
  }

  return (
    <div className="h-full overflow-y-auto bg-bg">
      <div className="mx-auto max-w-3xl px-4 py-4 sm:py-6">
        <Link to="/" className="inline-flex items-center gap-1.5 text-sm text-text-muted hover:text-text"><ArrowLeft className="size-4" /> Back to the dashboard</Link>
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
          <h2 id="gaps" className="text-sm font-semibold text-text">Known gaps in run 2 (expected, do not report as bugs)</h2>
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
            <span>{counts.pass} pass, {counts.fail_blocking + counts.fail_non_blocking} failed, {counts.skipped} skipped, {counts.todo} not yet</span>
            <label className="flex items-center gap-1.5 text-text">
              <input type="checkbox" checked={onlyNotes} onChange={(e) => setOnlyNotes(e.target.checked)} /> Only steps with notes or failures
            </label>
            <Button type="button" size="xs" onClick={copyReport}><ClipboardCopy /> Copy report</Button>
            <span role="status">{copied === 'ok' ? 'Report copied' : copied === 'fail' ? 'Could not copy' : ''}</span>
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

        <p className="mt-8 border-t border-border pt-3 text-xs text-text-faint">
          The install line in A1 is pinned to develop commit <code className="font-mono">{INSTALL_PIN.slice(0, 8)}</code>; the lead updates it when develop moves on.
        </p>
      </div>
    </div>
  )
}
