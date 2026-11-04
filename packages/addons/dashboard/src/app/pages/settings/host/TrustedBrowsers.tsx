import { useCallback, useEffect, useState } from 'react'
import { showsLogout } from '@/api/features'
import { getToken } from '@/api/bootstrap'
import { createFetchTransport } from '@/api/fetchTransport'
import { Section } from '../../ticket/shared'

interface Row { id: string; origin: string; created: number; last_used: number }

const transport = () => createFetchTransport('', getToken)
const when = (secs: number) => new Date(secs * 1000).toLocaleString()

/**
 * Settings: the browsers that signed in on this computer and may turn a hub switch into a full session without asking.
 * Revoking one sends its sessions back to read-only; a browser unused for 30 days is forgotten. Shown only on a full
 * local session (a hub-made session cannot list or revoke).
 */
export function TrustedBrowsers() {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [error, setError] = useState(false)
  const load = useCallback(async () => {
    if (showsLogout()) return // a hub or remote session is refused this route by design: no call
    try {
      const res = await transport().request('GET', '/api/trusted')
      setRows(res.status === 200 && Array.isArray(res.json) ? (res.json as Row[]) : null)
    } catch { setRows(null) }
  }, [])
  useEffect(() => { void load() }, [load])
  if (rows === null) return null
  const revoke = async (id: string) => {
    setError(false)
    try {
      const res = await transport().request('DELETE', `/api/trusted/${encodeURIComponent(id)}`)
      if (res.status !== 200) setError(true)
    } catch { setError(true) }
    await load()
  }
  return (
    <Section title="Trusted browsers">
      <p className="text-[13px] text-text-muted">Browsers where you signed in on this computer. A switch from your hub in one of them needs no extra step. One not used for 30 days is forgotten.</p>
      {rows.length === 0 ? <p data-testid="trusted-empty" className="mt-2 text-[13px] text-text-muted">No browser is trusted yet.</p> : (
        <ul aria-label="Trusted browsers" className="mt-2 divide-y divide-border text-[13px]">
          {rows.map((r) => (
            <li key={r.id} data-testid={`trusted-${r.id}`} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1"><div className="truncate">{r.origin}</div><div className="text-[12px] text-text-muted">Added {when(r.created)}. Last used {when(r.last_used)}.</div></div>
              <button type="button" className="rounded border border-border px-2 py-1" onClick={() => void revoke(r.id)}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {error && <p role="alert" className="mt-2 text-[13px] text-danger">Could not remove it. Try again.</p>}
    </Section>
  )
}
