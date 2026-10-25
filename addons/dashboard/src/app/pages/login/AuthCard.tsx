// The page and card the sign-in and add-this-device screens share (remote dashboard, #1198 R1-8, variant A).
import type { ReactNode } from 'react'
import { OrbitMark } from '@/brand/OrbitMark'
import { cn } from '@/lib/utils'

export function AuthPage({ children }: { children: ReactNode }) {
  return <main className="grid min-h-dvh place-items-center bg-bg px-4 py-8 text-text">{children}</main>
}

/** One minimal card: the mark, a title, plain words. Centered, 400 px at most, full width on a phone. */
export function AuthCard({ title, lead, children, className }: { title: string; lead?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <section className={cn('w-full max-w-[400px] rounded-[10px] border border-border bg-surface p-5 text-center sm:p-[22px]', className)} aria-labelledby="auth-title">
      <div className="mb-3.5 flex items-center justify-center gap-2 text-[17px] font-bold tracking-tight"><OrbitMark size={22} />orch</div>
      <h1 id="auth-title" className="text-lg font-semibold tracking-tight">{title}</h1>
      {lead && <p className="mt-1.5 text-sm text-text-muted">{lead}</p>}
      {children}
    </section>
  )
}

/** The round mark above a result (waiting, done, did not work). */
export function Badge({ tone, children }: { tone: 'ok' | 'bad' | 'wait'; children: ReactNode }) {
  return (
    <div aria-hidden className={cn('mx-auto mb-3 grid size-[54px] place-items-center rounded-full text-2xl', tone === 'ok' && 'bg-brand-soft text-brand', tone === 'bad' && 'bg-danger-soft text-danger', tone === 'wait' && 'bg-warning-soft text-warning motion-safe:animate-pulse')}>{children}</div>
  )
}

export function Notice({ tone, title, children }: { tone: 'err' | 'warn' | 'info'; title: string; children: ReactNode }) {
  return (
    <div role={tone === 'info' ? 'status' : 'alert'} className={cn('mt-4 flex gap-2.5 rounded-md border px-3 py-2.5 text-left text-[13px]', tone === 'err' && 'border-danger/40 bg-danger-soft', tone === 'warn' && 'border-warning/40 bg-warning-soft', tone === 'info' && 'border-info/35 bg-info-soft')}>
      <span aria-hidden className="font-bold">{tone === 'info' ? 'i' : '!'}</span>
      <div><b className="mb-0.5 block text-text">{title}</b><span className="text-text-muted">{children}</span></div>
    </div>
  )
}
