import { useId, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { api } from '@/api/client'
import { hubQueries, hubServed } from '@/api/hub'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

/** One name for the hub everywhere: "Acme hub" for Acme, but never "Hub hub" (a name that already says hub is used as is). */
export function hubTitle(name?: string): string {
  const n = (name ?? '').trim()
  if (!n) return 'Hub'
  return /\bhub\b/i.test(n) ? n : `${n} hub`
}
export const violet = 'border-chart-4/40 bg-chart-4/10 text-chart-4'
export const caveat = 'What the hub has already read cannot be taken back.'
/** The one-ticket dialog's caveat, naming the reader (P09): "What Ben Test has already seen cannot be taken back." */
export function compactCaveat(r: { people: string[]; teams: string[] }): string {
  const n = r.people.length + r.teams.length
  return `What ${n === 1 ? (r.people[0] ?? 'team ' + r.teams[0]) : 'people'} has already seen cannot be taken back.`.replace('people has', 'people have')
}
export function useHub(ws?: string) {
  return useQuery({ ...hubQueries.status(ws ?? ''), enabled: !!ws && hubServed(ws) })
}
/** Is One click on in this workspace? `unknown` until the signer answers (or when it cannot): never guessed. */
export type OneClick = 'on' | 'off' | 'unknown' | 'checking'
function useClickCard(ws: string) {
  return useQuery({ queryKey: ['click-status', ws], queryFn: () => api.clickStatus(ws), retry: false })
}
export function useOneClick(ws: string): OneClick {
  const status = useClickCard(ws)
  if (status.isPending) return 'checking'
  if (!status.data) return 'unknown'
  return status.data.enabled && status.data.available ? 'on' : 'off'
}
/** May the person switch this workspace to full hub mode? The host refuses it while One click is on or the key is in
 * One click custody (human.py refuse_full_while_click_on), in a terminal too: only One click off with the key under a
 * passphrase works. Not known counts as no. */
export function useFullAllowed(ws: string): boolean {
  const card = useClickCard(ws).data
  return !!card && !card.enabled && card.custody === 'passphrase'
}
/** After a hub step is signed: read the hub state again (the page behind the dialog changes). */
export function useHubRefresh(ws: string) {
  const qc = useQueryClient()
  return () => { void qc.invalidateQueries({ queryKey: ['hub', ws] }); void qc.invalidateQueries({ queryKey: ['click-status', ws] }) }
}
export function SettingsLink({ children }: { children: ReactNode }) {
  return <Link to="/settings/$tab" params={{ tab: 'general' }} className="underline" data-testid="hub-settings-link">{children}</Link>
}
export function StrictNeeds({ testId }: { testId?: string }) {
  return <p data-testid={testId}>Strict hub mode needs One click off and your key back under a passphrase. Do that first in <SettingsLink>Settings</SettingsLink>.</p>
}
/** Shown instead of a click step when One click is not on: say so plainly and offer the honest way on. */
export function NoOneClick({ state }: { state: OneClick; what: string; children?: ReactNode }) {
  if (state === 'checking') return <p role="status">Checking One click…</p>
  return <p data-testid="hub-no-one-click">One click is not ready. <SettingsLink>Turn on One click</SettingsLink>.</p>
}
export function Panel({ title, children, tone = '' }: { title: string; children: ReactNode; tone?: string }) {
  return <section className={'min-w-0 space-y-3 rounded-lg border border-border bg-surface p-4 text-sm ' + tone}>
    <h2 className="font-semibold">{title}</h2>{children}
  </section>
}
export function Action({ id, children, onClick, disabled = false, variant = 'outline' }: { id: string; children: ReactNode; onClick: () => void; disabled?: boolean; variant?: 'outline' | 'default' }) {
  return <Button data-testid={id} variant={variant} className="h-auto max-w-full whitespace-normal text-left" disabled={disabled} onClick={onClick}>{children}</Button>
}
export function Field({ label, value, onChange, hint, testId }: { label: string; value: string; onChange: (v: string) => void; hint?: string; testId: string }) {
  const id = useId()
  return <div className="min-w-0 space-y-1"><label htmlFor={id} className="block text-sm font-medium">{label}</label>
    <Input id={id} data-testid={testId} value={value} onChange={e => onChange(e.target.value)} aria-describedby={hint ? id + '-hint' : undefined} />
    {hint && <p id={id + '-hint'} className="text-xs text-text-muted">{hint}</p>}
  </div>
}
/** A small "Details" link: counts, ids and "not signed" notes stay one click away, never hidden from the person. */
export function Details({ children, testId }: { children: ReactNode; testId?: string }) {
  const [open, setOpen] = useState(false)
  return <div className="space-y-2">
    <button type="button" data-testid={testId} aria-expanded={open} className="text-[13px] text-text-muted underline" onClick={() => setOpen(v => !v)}>{open ? 'Hide details' : 'Details'}</button>
    {open && <div className="space-y-2 text-text-muted">{children}</div>}
  </div>
}
/** `cancel`: the footer label before that. `action`: the confirm button, next to Cancel. */
export function Flow({ title, onClose, children, finish = 'terminal', cancel, action }: { title: string; onClose: () => void; children: ReactNode; finish?: 'terminal' | 'here'; cancel?: string; action?: ReactNode }) {
  return <Dialog open onOpenChange={open => !open && onClose()}><DialogContent showCloseButton={false} className="data-[state=open]:animate-none data-[state=closed]:animate-none max-h-[85vh] overflow-y-auto sm:max-w-2xl">
    <DialogTitle>{title}</DialogTitle><DialogDescription className="sr-only">Review this change before confirming.</DialogDescription>
    <div className="min-w-0 space-y-4 text-sm break-words">{children}</div>
    {finish === 'terminal' && <div className="flex flex-wrap items-center justify-end gap-2"><Action id="hub-flow-cancel" variant="outline" onClick={onClose}>{cancel ?? 'Cancel'}</Action>{action}</div>}
  </DialogContent></Dialog>
}
