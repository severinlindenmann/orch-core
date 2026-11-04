// D5: only request ids and deadlines live in the browser. The signer owns every timer and signature.
import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'

type Hold = { ws: string; id: string; deadline: number }
const storageKey = 'orch-unsigned-holds'
const pending = new Map<string, Hold>()
const callbacks = new Map<string, () => void>()
function save() {
  try { localStorage.setItem(storageKey, JSON.stringify([...pending.values()])) } catch { /* unavailable storage */ }
}
export function trackHold(ws: string, id: string, deadline: number, done?: () => void) {
  pending.set(id, { ws, id, deadline })
  if (done) callbacks.set(id, done)
  save()
  show(pending.get(id)!)
}
function finish(hold: Hold, sent: boolean) {
  pending.delete(hold.id)
  const done = callbacks.get(hold.id)
  if (sent) done?.()
  callbacks.delete(hold.id)
  save()
  if (sent && done) { toast.dismiss(hold.id); return }
  toast[sent ? 'success' : 'info'](sent ? 'Sent' : 'Not sent', { id: hold.id, duration: 6000, action: undefined })
}
function show(hold: Hold) {
  const seconds = Math.max(0, Math.ceil(hold.deadline - Date.now() / 1000))
  toast.loading(seconds ? `Sending in ${seconds} s; closing this page still sends` : 'Checking whether it was sent…', {
    id: hold.id, duration: Infinity,
    action: { label: 'Undo', onClick: () => {
      void api.cancelClick(hold.ws, hold.id).then(got => {
        if (got.state === 'cancelled') finish(hold, false)
        else if (got.state === 'already_signed') toast.info('Already signed; checking delivery.', { id: hold.id })
      }).catch(() => toast.error('Could not cancel; checking delivery.', { id: hold.id }))
    } },
  })
}
export function HoldToasts() {
  const qc = useQueryClient()
  useEffect(() => {
    try {
      const stored: unknown = JSON.parse(localStorage.getItem(storageKey) ?? '[]')
      if (Array.isArray(stored)) for (const h of stored) {
        if (typeof h?.ws === 'string' && typeof h?.id === 'string' && typeof h?.deadline === 'number') pending.set(h.id, h)
      }
    } catch { /* no stored requests */ }
    let busy = false
    const check = async () => {
      if (busy) return
      busy = true
      try {
        for (const h of pending.values()) {
          show(h)
          try {
            const got = await api.stateClick(h.ws, h.id)
            if (got.state === 'submitted' || ['not_sent', 'cancelled', 'expired'].includes(got.state)) {
              finish(h, got.state === 'submitted')
              void qc.invalidateQueries()
            }
          } catch { toast.error('Could not confirm delivery. Reconnecting…', { id: h.id }) }
        }
      } finally { busy = false }
    }
    void check()
    const timer = setInterval(() => void check(), 1000)
    return () => clearInterval(timer)
  }, [qc])
  return null
}
