import { useId, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { api } from '@/api/client'
import { hubQueries, hubServed } from '@/api/hub'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

export const violet = 'border-chart-4/40 bg-chart-4/10 text-chart-4'
export const caveat = 'What the hub has already read cannot be taken back.'
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
export function NoOneClick({ state, what, children }: { state: OneClick; what: string; children?: ReactNode }) {
  if (state === 'checking') return <p role="status">Checking whether One click is on…</p>
  return <Panel title={state === 'off' ? 'One click is off' : 'One click is not available'} tone="border-warning/40 bg-warning-soft" >
    <p data-testid="hub-no-one-click">{state === 'off' ? `${what} here is signed with one click, and One click is off in this workspace.` : 'Could not tell whether One click is on.'} Turn it on in <SettingsLink>Settings</SettingsLink>, or do it in a terminal.</p>
    {children}
  </Panel>
}
export function Panel({ title, children, tone = '' }: { title: string; children: ReactNode; tone?: string }) {
  return <section className={'min-w-0 space-y-3 rounded-lg border border-border bg-surface p-4 text-sm ' + tone}>
    <h2 className="font-semibold">{title}</h2>{children}
  </section>
}
export function Action({ id, children, onClick, disabled = false }: { id: string; children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return <Button data-testid={id} variant="outline" className="h-auto max-w-full whitespace-normal text-left" disabled={disabled} onClick={onClick}>{children}</Button>
}
export function Field({ label, value, onChange, hint, testId }: { label: string; value: string; onChange: (v: string) => void; hint?: string; testId: string }) {
  const id = useId()
  return <div className="min-w-0 space-y-1"><label htmlFor={id} className="block text-sm font-medium">{label}</label>
    <Input id={id} data-testid={testId} value={value} onChange={e => onChange(e.target.value)} aria-describedby={hint ? id + '-hint' : undefined} />
    {hint && <p id={id + '-hint'} className="text-xs text-text-muted">{hint}</p>}
  </div>
}
export function Flow({ title, onClose, children, finish = 'terminal' }: { title: string; onClose: () => void; children: ReactNode; finish?: 'terminal' | 'here' }) {
  return <Dialog open onOpenChange={open => !open && onClose()}><DialogContent showCloseButton={false} className="data-[state=open]:animate-none data-[state=closed]:animate-none max-h-[85vh] overflow-y-auto sm:max-w-2xl">
    <div className="flex items-start justify-between gap-3"><DialogTitle>{title}</DialogTitle><Action id="hub-flow-close" onClick={onClose}>Close</Action></div><DialogDescription>{finish === 'here' ? 'Review it here, then confirm with one click.' : 'Review here. Finish in a terminal on your computer.'}</DialogDescription>
    <div className="min-w-0 space-y-4 text-sm [overflow-wrap:anywhere]">{children}</div>
  </DialogContent></Dialog>
}
