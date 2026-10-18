// A unified diff as lines: green added, red removed, grey context. The text is data: it is drawn as text, never as markup.
import { cn } from '@/lib/utils'
import { reveal } from './reveal'

export function Diff({ patch, label }: { patch: string; label: string }) {
  // git's own header lines (diff --git, index, ---, +++) mean nothing to a reader: the file is named above the box.
  // Only the lines before the first hunk are dropped; inside a hunk a removed "-- text" line must stay.
  let inHunk = false
  const lines = patch.split('\n').filter((l) => {
    if (l.startsWith('@@')) inHunk = true
    return inHunk || !(l.startsWith('diff ') || l.startsWith('index ') || l.startsWith('--- ') || l.startsWith('+++ '))
  })
  if (lines.at(-1) === '') lines.pop()
  return (
    <pre aria-label={label} className="max-h-[60vh] overflow-auto rounded-md border border-border bg-surface p-0 font-mono text-[12px] leading-5">
      {lines.map((line, i) => {
        const head = line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('--- ') || line.startsWith('+++ ') || line.startsWith('new file') || line.startsWith('deleted file')
        const kind = head ? 'head' : line.startsWith('@@') ? 'hunk' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'ctx'
        return (
          <span
            key={i}
            data-line={kind}
            className={cn(
              'block whitespace-pre px-3',
              kind === 'add' && 'bg-success/10 text-success',
              kind === 'del' && 'bg-danger/10 text-danger',
              kind === 'hunk' && 'bg-brand/10 text-text-muted',
              (kind === 'head' || kind === 'ctx') && 'text-text-muted',
            )}
          >
            {reveal(line) || ' '}
          </span>
        )
      })}
    </pre>
  )
}
