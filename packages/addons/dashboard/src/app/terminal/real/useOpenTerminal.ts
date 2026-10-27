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
    opening.current = true
    setBusy(true)
    const idem = newIdem()
    try {
      const data = await runsApi.preview(ws, args)
      if (data.blocked) { setPreview({ args, data, error: data.blocked.message ?? 'The host refused.', idem }); return }
      const { run } = await runsApi.open(ws, args, data.spec, idem)
      setPreview(null)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
      openDockOn(run.run)
    } catch (e) {
      setPreview({ args, data: null, error: e instanceof ApiError ? e.message : 'The host did not answer.', idem })
    } finally {
      opening.current = false
      setBusy(false)
    }
  }
  return { ask, preview, busy, onClose: () => setPreview(null) }
}
