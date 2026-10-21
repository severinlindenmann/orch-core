import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { newIdem, runsApi, type TermArgs, type TermPreview } from '@/api/runs'
import { ApiError } from '@/api/types'
import { openDockOn } from '@/app/terminal/dock/request'

export function useOpenTerminal(ws: string) {
  const qc = useQueryClient()
  const [preview, setPreview] = useState<{ args: TermArgs; data: TermPreview | null; error: string; idem: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const opening = useRef(false)
  const ask = async (args: TermArgs) => {
    if (opening.current) return
    const idem = newIdem()
    setPreview({ args, data: null, error: '', idem })
    try {
      const data = await runsApi.preview(ws, args)
      setPreview((p) => p?.idem === idem ? { ...p, data } : p)
    } catch (e) {
      setPreview((p) => p?.idem === idem ? { ...p, error: e instanceof ApiError ? e.message : 'The host did not answer.' } : p)
    }
  }
  const confirm = async () => {
    if (!preview?.data || preview.data.blocked || opening.current) return
    opening.current = true
    setBusy(true)
    try {
      const { run } = await runsApi.open(ws, preview.args, preview.data.spec, preview.idem)
      setPreview(null)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
      openDockOn(run.run)
    } catch (e) {
      setPreview((p) => p ? { ...p, error: e instanceof ApiError ? e.message : 'The host did not answer.' } : p)
    } finally {
      opening.current = false
      setBusy(false)
    }
  }
  return { ask, preview, busy, onConfirm: () => void confirm(), onClose: () => setPreview(null) }
}
