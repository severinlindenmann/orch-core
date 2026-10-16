import type { ReactNode } from 'react'
import type { SignContent as Content } from '@/api/signing'
import { Raw } from './visible'

export function SignDetails({ children }: { children: ReactNode }) {
  return <details className="space-y-3 text-[12px] text-text-muted"><summary className="cursor-pointer">Technical details</summary>{children}</details>
}
// Keep host-flattened ⏎ markers verbatim. Literal newlines (older hosts and block
// text) stay inside the value, with a visible mark and indentation on each line.
function Value({ children, block = false }: { children: string; block?: boolean }) {
  const lines = children.split('\n')
  if (!block && lines.length === 1) return <Raw>{children}</Raw>
  return <span className="inline-block max-w-full align-top">
    {lines.map((line, i) => <span key={i} className="flex gap-2 pl-4">
      <span aria-hidden="true" className="text-text-muted">|</span>
      <Raw>{line}</Raw>
    </span>)}
  </span>
}
export function SignContent({ content }: { content: Content }) {
  return <div className="max-h-[38vh] space-y-4 overflow-auto text-[13px] [overflow-wrap:anywhere]">
    {content.ticket && <div>
      <p><Raw>{content.ticket.key}</Raw> · …<Raw>{content.ticket.uid_tail}</Raw></p>
      <p className="text-[11px] text-text-muted"><Value>{content.ticket.key_note}</Value></p>
      {content.ticket.title && <Value>{content.ticket.title}</Value>}
    </div>}
    {content.sections?.map(s => <section key={s.id}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Raw>{s.title}</Raw></h3><Value block>{s.text}</Value></section>)}
    {!!content.acceptance?.length && <section><h3 className="text-text-muted">Acceptance criteria</h3><ul className="space-y-1">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Raw>{a.id}</Raw> · <Value>{a.text}</Value></li>)}</ul></section>}
    {!!content.tasks?.length && <section><h3 className="text-text-muted">Plan tasks</h3><ol className="list-decimal space-y-2 pl-5">{content.tasks.map(t => <li key={t.n} value={t.n}><Raw>{t.id}</Raw> · <Value>{t.title}</Value>{t.verify?.map((v,i) => <p key={i} className="font-mono text-[12px]">Verify: <Value>{v}</Value></p>)}</li>)}</ol></section>}
    {!!content.fields_changed?.length && <section><h3 className="text-text-muted">Changed fields</h3>{content.fields_changed.map(f => <p key={f.field}><Raw>{f.field}</Raw>: <Value>{f.before ?? '—'}</Value>{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → <Value>{f.after ?? '—'}</Value></p>)}</section>}
    {[...(content.notes ?? []), ...(Array.isArray(content.note) ? content.note : content.note ? [content.note] : [])].map((note, i) => <p key={i}><Value>{note}</Value></p>)}
  </div>
}
