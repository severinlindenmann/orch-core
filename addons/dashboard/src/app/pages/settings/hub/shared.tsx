import { useId, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { hubQueries, hubServed } from '@/api/hub'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

export const violet = 'border-chart-4/40 bg-chart-4/10 text-chart-4'
export const caveat = 'What the hub has already read cannot be taken back.'
export function useHub(ws?: string) {
  return useQuery({ ...hubQueries.status(ws ?? ''), enabled: !!ws && hubServed(ws) })
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
export function Flow({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return <Dialog open onOpenChange={open => !open && onClose()}><DialogContent showCloseButton={false} className="data-[state=open]:animate-none data-[state=closed]:animate-none max-h-[85vh] overflow-y-auto sm:max-w-2xl">
    <div className="flex items-start justify-between gap-3"><DialogTitle>{title}</DialogTitle><Action id="hub-flow-close" onClick={onClose}>Close</Action></div><DialogDescription>Review here. Finish in Terminal on your computer.</DialogDescription>
    <div className="min-w-0 space-y-4 text-sm [overflow-wrap:anywhere]">{children}</div>
  </DialogContent></Dialog>
}
