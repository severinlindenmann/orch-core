// The right-hand side of the explorer: one file as a preview, or its history. Read only: nothing here can change a file.
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Lock } from 'lucide-react'
import { explorerQueries, lockedText, type FileView } from '@/api/explorer'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { HistoryPanel } from './History'
import { reveal } from './reveal'

const size = (n: number | undefined) => (n === undefined ? '' : n < 1024 ? `${n} bytes` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)

function Code({ text }: { text: string }) {
  const lines = text.split('\n')
  if (lines.at(-1) === '') lines.pop()
  return (
    <div className="max-h-[70vh] overflow-auto rounded-md border border-border bg-surface" data-testid="code">
      <pre className="m-0 min-w-max p-0 font-mono text-[12px] leading-5">
        {lines.map((line, i) => (
          <span key={i} className="flex">
            <span aria-hidden className="w-12 shrink-0 select-none pr-3 text-right text-text-faint">
              {i + 1}
            </span>
            <bdi className="whitespace-pre pr-3 [unicode-bidi:isolate]">{reveal(line) || ' '}</bdi>
          </span>
        ))}
      </pre>
    </div>
  )
}

function Preview({ file }: { file: FileView }) {
  switch (file.kind) {
    case 'text':
      return (
        <div className="space-y-2">
          {file.hidden_chars && (
            <p role="note" className="rounded-md border border-warning bg-warning-soft px-3 py-2 text-[13px] text-warning" data-testid="hidden-chars">
              This file has characters you cannot see (shown as ⟦u+…⟧). In code they can make it do something other than what it reads like.
            </p>
          )}
          <Code text={file.text ?? ''} />
        </div>
      )
    case 'markdown':
      return (
        <div className="space-y-2">
          {file.hidden_chars && <p className="text-[12px] text-warning">This file has characters you cannot see.</p>}
          <div className="rounded-md border border-border bg-surface p-4" data-testid="markdown">
            <SafeMarkdown text={file.text ?? ''} />
          </div>
        </div>
      )
    case 'image':
      return (
        <div className="rounded-md border border-border bg-surface p-3">
          <img src={`data:${file.mime};base64,${file.data}`} alt={file.label} className="mx-auto max-h-[70vh] max-w-full object-contain" />
        </div>
      )
    case 'locked':
      return (
        <p className="flex items-start gap-2 rounded-md border border-border bg-surface p-3 text-[13px] text-text-muted" data-testid="locked">
          <Lock className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{lockedText(file.reason)}</span>
        </p>
      )
    case 'binary':
      return <p className="text-[13px] text-text-muted">This is not a text file, so there is no preview. {size(file.size)}.</p>
    case 'too_large':
      return <p className="text-[13px] text-text-muted">This file is too large to preview ({size(file.size)}).</p>
    case 'empty':
      return <p className="text-[13px] text-text-muted">This file is empty.</p>
    case 'link':
      return <p className="text-[13px] text-text-muted">This is a link to another place. Links are never followed here.</p>
    case 'dir':
      return <p className="text-[13px] text-text-muted">This is a folder.</p>
    default:
      return <p className="text-[13px] text-text-muted">This kind of file cannot be shown.</p>
  }
}

export interface ViewerState {
  root: string
  path: string
  rev?: string
  view?: 'history'
  sha?: string
}

export function Viewer({ ws, state, onChange, onBack }: { ws: string; state: ViewerState; onChange: (patch: Partial<ViewerState>) => void; onBack: () => void }) {
  const q = useQuery(explorerQueries.file(ws, state.root, state.path, state.rev))
  const history = state.view === 'history'
  const file = q.data
  return (
    <section aria-label="File" className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="ghost" className="h-8 gap-1 lg:hidden" onClick={onBack}>
          <ArrowLeft className="size-4" aria-hidden /> Files
        </Button>
        <h2 className="min-w-0 flex-1 break-all text-[14px] font-medium">
          <bdi>{file?.label ?? state.path.split('/').at(-1)}</bdi>
        </h2>
        <div role="tablist" aria-label="View" className="inline-flex rounded-md border border-border p-0.5">
          {(['Preview', 'History'] as const).map((t) => {
            const on = (t === 'History') === history
            return (
              <button key={t} role="tab" aria-selected={on} className={cn('rounded px-3 py-1 text-[13px]', on ? 'bg-brand-soft text-text' : 'text-text-muted hover:text-text')} onClick={() => onChange({ view: t === 'History' ? 'history' : undefined, sha: undefined })}>
                {t}
              </button>
            )
          })}
        </div>
      </div>
      <p className="break-all text-[12px] text-text-muted">
        <bdi>{state.path}</bdi>
        {file?.size !== undefined && file.kind !== 'locked' ? ` · ${size(file.size)}` : ''}
        {file?.modified ? ` · changed ${fmtDateTime(file.modified)}` : ''}
      </p>
      {state.rev && (
        <p className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-brand-soft px-3 py-2 text-[13px]" data-testid="old-version">
          This is an older version of the file.
          <Button size="sm" variant="outline" className="h-7 text-[12px]" onClick={() => onChange({ rev: undefined })}>
            Show the current file
          </Button>
        </p>
      )}
      {history ? (
        file?.kind === 'locked' ? (
          <Preview file={file} />
        ) : (
          <HistoryPanel ws={ws} root={state.root} path={state.path} sha={state.sha} onSha={(sha) => onChange({ sha })} onOpenVersion={(rev) => onChange({ rev, view: undefined, sha: undefined })} />
        )
      ) : q.isError ? (
        <LoadFailed what="this file" onRetry={() => void q.refetch()} />
      ) : file ? (
        <Preview file={file} />
      ) : (
        <p className="text-[13px] text-text-muted">Loading…</p>
      )}
    </section>
  )
}
