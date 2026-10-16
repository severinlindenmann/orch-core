import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { methodLabel, type SignMethod } from '@/api/signing'
import { Button } from '@/components/ui/button'

export function useSignMethods(ws: string | undefined) {
  const settings = useQuery({ ...queries.hostSettings(ws ?? ''), enabled: !!ws, retry: false })
  const preference = useQuery({ ...queries.signDefault(ws ?? ''), enabled: !!ws, retry: false })
  const allowed = settings.data?.sign_methods
  return { allowed, preference: preference.data, loading: !!ws && (settings.isPending || preference.isPending) }
}
export function OtherWays({ allowed, current, onSelect, disabled = false }: { allowed: SignMethod[]; current: SignMethod; onSelect: (method: SignMethod) => void; disabled?: boolean }) {
  return <details className="text-[13px]"><summary className="cursor-pointer text-text-muted">Other ways ▾</summary><div className="flex flex-wrap gap-2 py-2">{allowed.filter(m => m !== current).map(m => <Button key={m} variant="outline" disabled={disabled} onClick={() => onSelect(m)}>{methodLabel[m]}</Button>)}</div></details>
}
export function PhoneWaiting() {
  return <div role="status" className="space-y-2 rounded-md border border-border p-5 text-center"><p className="font-medium">Waiting for your phone…</p><p className="text-[13px] text-text-muted">Phone approval is not connected yet. No notification was sent and nothing has been signed. Choose another way to continue.</p></div>
}
