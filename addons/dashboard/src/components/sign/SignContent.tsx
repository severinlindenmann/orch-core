import type { ReactNode } from 'react'
import type { SignContent as Content } from '@/api/signing'
import { Prose, Raw } from './visible'

export function SignDetails({ children }: { children: ReactNode }) {
  return <details className="space-y-3 text-[12px] text-text-muted"><summary className="cursor-pointer">Details</summary>{children}</details>
}
export function SignContent({ content }: { content: Content }) {
  return <div className="max-h-[38vh] space-y-4 overflow-auto text-[13px] [overflow-wrap:anywhere]">
    {content.sections?.map(s => <section key={s.id}><h3 className="mb-1 text-[12px] font-medium text-text-muted"><Raw>{s.title}</Raw></h3><Prose>{s.text}</Prose></section>)}
    {!!content.acceptance?.length && <section><h3 className="text-text-muted">Acceptance criteria</h3><ul className="space-y-1">{content.acceptance.map(a => <li key={a.id}><span aria-label={a.checked ? 'Checked' : 'Unchecked'}>{a.checked ? '☑' : '☐'}</span> <Raw>{a.id}</Raw> · <Prose inline>{a.text}</Prose></li>)}</ul></section>}
    {!!content.tasks?.length && <section><h3 className="text-text-muted">Plan tasks</h3><ol className="list-decimal space-y-2 pl-5">{content.tasks.map(t => <li key={t.n} value={t.n}><Prose inline>{t.title}</Prose>{t.verify?.map((v,i) => <p key={i} className="font-mono text-[12px]">Verify: <Raw>{v}</Raw></p>)}</li>)}</ol></section>}
    {!!content.fields_changed?.length && <section><h3 className="text-text-muted">Changed fields</h3>{content.fields_changed.map(f => <p key={f.field}><Raw>{f.field}</Raw>: <Raw>{f.before ?? '—'}</Raw>{f.before_note && <span className="text-text-muted"> (<Raw>{f.before_note}</Raw>)</span>} → <Raw>{f.after ?? '—'}</Raw></p>)}</section>}
    {content.note && <Prose>{content.note}</Prose>}
  </div>
}
