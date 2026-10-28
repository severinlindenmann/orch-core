import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { newIdem, runsApi, type Run, type TermArgs, type TermPreview } from '@/api/runs'
import { ApiError } from '@/api/types'
import { openDockOn } from '@/app/terminal/dock/request'

export interface OpenAttempt { args: TermArgs; data: TermPreview | null; error: string; idem: string }

/** New terminal in one press: the host's preview, then open with its spec. A failed open keeps its idem and spec, so
 * Try again returns the same run, never a second one. A blocked preview is shown and cannot be retried as is. */
export function useOpenTerminal(ws: string, onOpened: (run: Run) => void = (run) => openDockOn(run.run)) {
  const qc = useQueryClient()
  const [preview, setPreview] = useState<OpenAttempt | null>(null)
  const [busy, setBusy] = useState(false)
  const opening = useRef(false)
  const go = async (args: TermArgs, idem: string, known: TermPreview | null) => {
    if (!ws || opening.current) return
    opening.current = true
    setBusy(true)
    let data = known
    try {
      data ??= await runsApi.preview(ws, args)
      if (data.blocked) { setPreview({ args, data, error: data.blocked.message ?? 'The host refused.', idem }); return }
      const { run } = await runsApi.open(ws, args, data.spec, idem)
      setPreview(null)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
      onOpened(run)
    } catch (e) {
      setPreview({ args, data, error: e instanceof ApiError ? e.message : 'The host did not answer.', idem })
    } finally {
      opening.current = false
      setBusy(false)
    }
  }
  const ask = (args: TermArgs) => go(args, newIdem(), null)
  const retry = preview && !preview.data?.blocked ? () => void go(preview.args, preview.idem, preview.data) : undefined
  return { ask, preview, busy, onRetry: retry, onClose: () => setPreview(null) }
}
