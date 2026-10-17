import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { PhoneHint } from '@/api/phone'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { methodLabel, type SignMethod } from '@/api/signing'
import { Button } from '@/components/ui/button'

export function useSignMethods(ws: string | undefined) {
  const settings = useQuery({ ...queries.hostSettings(ws ?? ''), enabled: !!ws, retry: false })
  const preference = useQuery({ ...queries.signDefault(ws ?? ''), enabled: !!ws, retry: false })
  const unrestricted = settings.data?.sign_methods === null
  const allowed: SignMethod[] | undefined = unrestricted ? ['terminal', 'click', 'phone'] : settings.data?.sign_methods ?? undefined
  return { allowed, unrestricted, preference: preference.data, preferenceFailed: preference.isError, loading: !!ws && (settings.isPending || preference.isPending) }
}
export function OtherWays({ allowed, current, onSelect, disabled = false, phoneAvailable = false, phonePhaseOne = false }: { allowed: SignMethod[]; current: SignMethod; onSelect: (method: SignMethod) => void; disabled?: boolean; phoneAvailable?: boolean; phonePhaseOne?: boolean }) {
  const others = allowed.filter(m => m !== current && (m !== 'phone' || phoneAvailable || phonePhaseOne))
  if (others.length === 0) return null
  return <details className="text-[13px]"><summary className="cursor-pointer text-text-muted">Other ways <span aria-hidden="true">▾</span></summary><div className="flex flex-wrap gap-2 py-2">{others.map(m => <Button key={m} variant="outline" disabled={disabled || (m === 'phone' && !phoneAvailable)} onClick={() => onSelect(m)}>{methodLabel[m]}{m === 'phone' && !phoneAvailable && ' · pair a phone first'}</Button>)}</div></details>
}
export function PhoneWaiting({ hint }: { hint: PhoneHint }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [])
  const seconds = Math.max(0, Math.ceil((Date.parse(hint.expires_at) - now) / 1000))
  const copy = hint.state === 'done' ? hint.outcome?.kind === 'approved' ? 'Approved.' : 'Changes requested.'
    : hint.state === 'stale' ? 'This changed while you looked. Reopen it.'
      : hint.state === 'expired' ? 'This phone request expired. Reopen it to send another request.'
        : hint.state === 'cancelled' ? 'Phone request cancelled.' : 'Waiting for your phone…'
  return <div role="status" className="space-y-2 rounded-md border border-border p-5 text-center">
    <p className="flex items-center justify-center gap-2 font-medium">{hint.state === 'open' && <Loader2 className="size-4 animate-spin" aria-hidden />}{copy}</p>
    {hint.state === 'open' && <><p>{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, '0')} remaining</p><p className="text-[13px] text-text-muted">Open orch on your phone. Review the current text and choose Approve or Request changes there.</p></>}
    {(hint.state === 'cancelled' || hint.state === 'expired') && <p className="text-[13px] text-text-muted">A decision already on its way can still arrive.</p>}
  </div>
}
