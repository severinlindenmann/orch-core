// A lossless, line-by-line restyle of text a person is asked to sign (a requirements or plan section). Each line is
// classified by its LEADING marker only ("- " / "* " bullet, "N. " numbered item, "# " bold line, empty line = a gap,
// anything else = a paragraph line). The rest of the line is drawn verbatim through `prose()` (hidden and bidi
// characters made visible): no inline markup (no bold from "*", no links, no images, no HTML), nothing merged,
// dropped or reordered. A markdown renderer would hide characters (comments, link targets, escapes), so it is not used here.
import { prose } from './visible'

export type SignedLine =
  | { kind: 'gap'; text: '' }
  | { kind: 'bullet'; text: string }
  | { kind: 'number'; text: string; n: string }
  | { kind: 'heading'; text: string }
  | { kind: 'para'; text: string }

/** Classify every line of `text` (split on "\n" only). `text` of a line is the line minus its stripped leading marker. */
export function classifyLines(text: string): SignedLine[] {
  return text.split('\n').map((line): SignedLine => {
    if (line === '') return { kind: 'gap', text: '' }
    let m = /^[-*] (.*)$/s.exec(line)
    if (m) return { kind: 'bullet', text: m[1] }
    m = /^(\d{1,9})\. (.*)$/s.exec(line)
    if (m) return { kind: 'number', n: m[1], text: m[2] }
    m = /^#{1,6} (.*)$/s.exec(line)
    if (m) return { kind: 'heading', text: m[1] }
    return { kind: 'para', text: line }
  })
}

const T = 'min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] [unicode-bidi:isolate]'

/** The lines as styled rows. Headings are bold body text (never larger than orch's own headings). */
export function SignedTextView({ text, className }: { text: string; className?: string }) {
  return (
    <div data-testid="signed-text" className={`space-y-1 font-sans text-[13px] leading-relaxed text-text ${className ?? ''}`}>
      {classifyLines(text).map((l, i) => {
        if (l.kind === 'gap') return <div key={i} data-line="gap" aria-hidden className="h-2" />
        const body = <bdi data-text className={T}>{prose(l.text)}</bdi>
        if (l.kind === 'bullet')
          return (
            <div key={i} data-line="bullet" className="flex gap-2">
              <span aria-hidden className="mt-[0.45em] size-1.5 shrink-0 rounded-full bg-text-muted" />
              {body}
            </div>
          )
        if (l.kind === 'number')
          return (
            <div key={i} data-line="number" className="flex gap-2">
              <span data-marker className="mt-0.5 flex h-5 min-w-5 w-auto shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 px-1.5 font-mono text-[11px] text-text-muted [unicode-bidi:isolate]">{l.n}</span>
              {body}
            </div>
          )
        if (l.kind === 'heading')
          return (
            <div key={i} data-line="heading" className="pt-1 font-semibold">
              {body}
            </div>
          )
        return (
          <div key={i} data-line="para">
            {body}
          </div>
        )
      })}
    </div>
  )
}
