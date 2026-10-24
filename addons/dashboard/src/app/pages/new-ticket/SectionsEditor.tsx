import { ChevronRight, Eye, ImagePlus, Pencil, Plus, X } from 'lucide-react'
import { useId, useRef, useState, type ClipboardEvent, type DragEvent, type MutableRefObject, type ReactNode } from 'react'
import { ArtifactImagesContext } from '@/addon-ui/artifactImagesContext'
import { MAX_IMAGES, PasteError, pastedIn, pastedUrls, preparePaste, referenceFor } from '@/api/pasteImage'
import { SECTIONS_BY_TYPE, SECTION_ORDER, neededBefore, requiredAtCreation, sectionLabel, type SectionName } from '@/api/sections'
import type { BodySections, TicketType } from '@/api/types'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

const HINT: Partial<Record<SectionName, string>> = {
  summary: 'A short paragraph. Optional here.',
  context: 'Why this matters and what is known today.',
  requirements: 'What must be true when this is done. One requirement per line works well.',
  out_of_scope: 'What this deliberately does not cover.',
  plan: 'Steps to take. The agent usually drafts this.',
  decisions: 'Choices already made and why.',
  verification: 'How someone will check the result.',
  current_state: 'Where things stand right now.',
}

export function Section({ type, name, value, onChange, error }: { type: TicketType; name: SectionName; value: string; onChange: (v: string) => void; error?: string }) {
  const id = useId()
  const [preview, setPreview] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  const [pasteNote, setPasteNote] = useState<string | null>(null)
  const pictures = pastedIn([value])
  const imageFiles = (dt: DataTransfer | null) => [...(dt?.files ?? [])].filter((f) => f.type.startsWith('image/'))
  // R3 part B: the browser decodes the picture and draws it again as PNG, then puts `![screenshot](artifact:NAME)` at the cursor.
  const take = async (files: File[]) => {
    setPasteNote(null)
    let text = value
    let at = area.current?.selectionStart ?? text.length
    let end = area.current?.selectionEnd ?? at
    let count = pictures.length
    try {
      for (const f of files) {
        if (count >= MAX_IMAGES) throw new PasteError(`At most ${MAX_IMAGES} pictures fit in one signing run.`)
        const p = await preparePaste(f)
        const ref = `${at > 0 && text[at - 1] !== '\n' ? '\n' : ''}${referenceFor(p)}\n`
        text = text.slice(0, at) + ref + text.slice(end)
        at = end = at + ref.length
        count += 1
      }
    } catch (e) {
      setPasteNote(e instanceof PasteError ? e.message : 'This picture could not be added.')
    }
    if (text !== value) onChange(text)
  }
  const onPaste = (e: ClipboardEvent) => {
    const files = imageFiles(e.clipboardData)
    if (!files.length) return
    e.preventDefault()
    void take(files)
  }
  const onDrop = (e: DragEvent) => {
    const files = imageFiles(e.dataTransfer)
    if (!files.length) return
    e.preventDefault()
    void take(files)
  }
  const everyday = name === 'requirements' && type !== 'spike'
  const label = everyday ? 'What should be true when this is done?' : sectionLabel(type, name)
  const needGate = neededBefore(type, name)
  const atCreation = requiredAtCreation(type).includes(name)
  return (
    <section className="space-y-1.5">
      <div className="flex items-center gap-2">
        <label htmlFor={id} className="text-[13px] font-medium">
          {label}
        </label>
        <span id={`${id}-need`} className="text-[11px] text-text-faint">
          {atCreation ? 'required' : needGate ? `needed before the ${needGate} gate` : 'optional'}
        </span>
        <div className="flex-1" />
        <Button type="button" variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]" aria-pressed={preview} onClick={() => setPreview((p) => !p)}>
          {preview ? <Pencil className="size-3" /> : <Eye className="size-3" />}
          {preview ? 'Edit' : 'Preview'}
        </Button>
      </div>
      {preview ? (
        <div className="min-h-20 rounded-md border border-border bg-surface px-3 py-2" aria-label={`${label} preview`}>
          {value.trim() ? (
            <ArtifactImagesContext.Provider value={pastedUrls(value)}>
              <SafeMarkdown text={value} />
            </ArtifactImagesContext.Provider>
          ) : <p className="text-[13px] text-text-faint">Nothing to preview.</p>}
        </div>
      ) : (
        <Textarea
          id={id}
          ref={area}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onPaste={onPaste}
          onDrop={onDrop}
          placeholder={everyday ? 'Describe the result you want. One line per thing works well.' : HINT[name]}
          aria-describedby={error ? `${id}-need ${id}-error` : `${id}-need`}
          aria-required={atCreation}
          aria-invalid={!!error || undefined}
          className="min-h-20 font-mono text-[13px] md:text-[13px]"
        />
      )}
      {pictures.length > 0 && (
        <ul data-testid="paste-strip" aria-label={`${label} pictures`} className="flex flex-wrap gap-2">
          {pictures.map((p) => (
            <li key={p.name} className="relative">
              <img src={pastedUrls(value)[p.name]} alt={p.alt} className="h-14 max-w-32 rounded-md border border-border object-cover" />
              <Button
                type="button"
                variant="secondary"
                size="icon"
                className="absolute -right-1.5 -top-1.5 size-5 rounded-full"
                aria-label={`Remove picture ${p.alt}`}
                onClick={() => onChange(value.split('\n').filter((l) => !l.includes(`(artifact:${p.name})`)).join('\n'))}
              >
                <X className="size-3" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      {!preview && (
        <p className="flex items-center gap-1 text-[11px] text-text-faint">
          <ImagePlus className="size-3" aria-hidden /> Paste or drop a screenshot to add it here.
        </p>
      )}
      {pasteNote && (
        <p role="alert" data-testid="paste-note" className="text-[12px] text-danger">
          {pasteNote}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-[12px] text-danger">
          {error}
        </p>
      )}
    </section>
  )
}

export function SectionsEditor({
  type,
  sections,
  onSection,
  acceptance,
  onAcceptance,
  invalid,
  collapsible = false,
  flushRef,
  extra,
}: {
  type: TicketType
  sections: BodySections
  onSection: (name: SectionName, text: string) => void
  acceptance: string[]
  onAcceptance: (list: string[]) => void
  /** Why a section cannot be submitted yet, by section (shown under it). */
  invalid: Partial<Record<SectionName, string>>
  /** The sections needed at creation stay open; the others and the acceptance criteria fold under "More sections". */
  collapsible?: boolean
  /** E-7: the owner of the form asks for a criterion typed but not yet added (returned trimmed, and cleared here). */
  flushRef?: MutableRefObject<(() => string) | null>
  /** More choices (priority, labels, people ...) shown inside the same "More options" fold. */
  extra?: ReactNode
}) {
  const [next, setNext] = useState('')
  const pending = useRef('')
  pending.current = next
  if (flushRef) flushRef.current = () => { const t = pending.current.trim(); if (t) setNext(''); return t }
  const shown = SECTION_ORDER.filter((n) => SECTIONS_BY_TYPE[type][n] !== 'absent')
  const first = collapsible ? shown.filter((n) => requiredAtCreation(type).includes(n)) : shown
  const rest = shown.filter((n) => !first.includes(n))
  // Starts open when a folded section already has text (a restored draft), so nothing typed is hidden.
  const [more, setMore] = useState(() => !collapsible || rest.some((n) => sections[n]?.trim()) || acceptance.length > 0)
  const moreId = useId()
  const add = () => {
    const t = next.trim()
    if (!t) return
    onAcceptance([...acceptance, t])
    setNext('')
  }
  return (
    <div className="space-y-5">
      {first.map((n) => (
        <Section key={n} type={type} name={n} value={sections[n] ?? ''} onChange={(v) => onSection(n, v)} error={invalid[n]} />
      ))}
      {collapsible && (
        <button
          type="button"
          aria-expanded={more}
          aria-controls={moreId}
          onClick={() => setMore((m) => !m)}
          className="flex w-full min-w-0 items-center gap-1.5 rounded-md text-left text-[13px] font-medium text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
        >
          <ChevronRight aria-hidden className={`size-4 transition-transform motion-reduce:transition-none ${more ? 'rotate-90' : ''}`} />
          <span className="shrink-0">More options</span>
        </button>
      )}
      {more && (
        <div id={moreId} className="space-y-5">
          {rest.map((n) => (
            <Section key={n} type={type} name={n} value={sections[n] ?? ''} onChange={(v) => onSection(n, v)} error={invalid[n]} />
          ))}
          <section className="space-y-1.5" aria-labelledby="ac-heading">
            <h2 id="ac-heading" className="text-[13px] font-medium">
              Acceptance criteria
            </h2>
            {acceptance.length > 0 && (
              <ol className="space-y-1">
                {acceptance.map((text, i) => (
                  <li key={i} className="flex items-center gap-2 rounded-md border border-border bg-surface px-2.5 py-1.5 text-[13px]">
                    <span className="font-mono text-[11px] text-text-faint">AC{i + 1}</span>
                    <span className="flex-1">{text}</span>
                    <Button type="button" variant="ghost" size="icon" className="size-6" aria-label={`Remove AC${i + 1}`} onClick={() => onAcceptance(acceptance.filter((_, j) => j !== i))}>
                      <X className="size-3.5" />
                    </Button>
                  </li>
                ))}
              </ol>
            )}
            <div className="flex gap-2">
              <Input
                value={next}
                onChange={(e) => setNext(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) {
                    e.preventDefault()
                    add()
                  }
                }}
                placeholder="Something that must be provable"
                aria-label="New acceptance criterion"
                className="h-8 text-[13px]"
              />
              <Button type="button" variant="outline" size="sm" onClick={add}>
                <Plus />
                Add
              </Button>
            </div>
          </section>
          {extra}
        </div>
      )}
    </div>
  )
}
