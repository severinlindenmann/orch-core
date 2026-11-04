// What stands where a preview cannot: never a blank panel. The file, why there is no preview, and what to do.
import { Check, Copy, FileQuestion } from 'lucide-react'
import { useState } from 'react'
import type { Artifact } from '@/api/types'
import { Button } from '@/components/ui/button'
import { visible } from '@/components/sign/visible'
import { fmtBytes, shortHash } from './shared'

export function NoPreviewCard({ a, why, warn = false, quiet = false }: { a: Pick<Artifact, 'name' | 'kind' | 'bytes' | 'sha256'>; why: string; warn?: boolean; /** An empty file or a check result: the sentence only; the file facts and the hash sit behind Details. */ quiet?: boolean }) {
  const [copied, setCopied] = useState<'idle' | 'ok' | 'fail'>('idle')
  const copy = () => {
    if (!navigator.clipboard?.writeText) return setCopied('fail')
    navigator.clipboard.writeText(a.name).then(() => setCopied('ok'), () => setCopied('fail'))
  }
  if (quiet)
    return (
      <div data-testid="no-preview-card" className="space-y-1.5 rounded-md border border-dashed border-border bg-surface-2 px-3 py-2.5 text-[13px]">
        <p className="text-text">{why}</p>
        <details className="text-[12px] text-text-muted">
          <summary className="cursor-pointer">Details</summary>
          <p className="mt-1">
            <span className="break-all font-mono">{visible(a.name)}</span> · {a.kind}
            {a.bytes != null && ` · ${fmtBytes(a.bytes)}`} · sha256 {shortHash(a.sha256, 12)}
          </p>
          <p>The sha256 is what an approval binds. The file stays with the ticket on the host.</p>
        </details>
      </div>
    )
  return (
    <div data-testid="no-preview-card" role={warn ? 'alert' : undefined} className={`space-y-1.5 rounded-md border px-3 py-2.5 text-[13px] ${warn ? 'border-danger/40 bg-danger-soft' : 'border-dashed border-border bg-surface-2'}`}>
      <p className="flex items-center gap-2 font-medium">
        <FileQuestion className="size-4 shrink-0 text-text-muted" aria-hidden />
        <span className="min-w-0 break-all font-mono text-[12px]">{visible(a.name)}</span>
      </p>
      <p className="text-[12px] text-text-muted">
        {a.kind}
        {a.bytes != null && ` · ${fmtBytes(a.bytes)}`} · sha256 {shortHash(a.sha256, 12)}
      </p>
      <p className={warn ? 'text-danger' : 'text-text'}>{why}</p>
      <div className="flex flex-wrap items-center gap-2 text-[12px] text-text-muted">
        <span>The sha256 is what an approval binds. The file stays with the ticket on the host.</span>
        <Button type="button" variant="outline" size="sm" className="h-6 px-2 text-[12px]" onClick={copy}>
          {copied === 'ok' ? <Check /> : <Copy />}
          Copy file name
        </Button>
        {copied === 'fail' && <span role="status">Could not copy. Select the name and copy it instead.</span>}
      </div>
    </div>
  )
}
