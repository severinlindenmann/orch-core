import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { SIGNING_METHOD_STALE } from '@/api/types'

/** The click-to-sign switch is flipped in the terminal, which no stream announces. A refusal that shows the page
 * guessed the way to sign wrongly re-reads the workspace rows and the allowed methods, so the dialog changes variant
 * without a reload (the next prepare then follows the host's current answer). */
export function useSigningMethodRefresh() {
  const qc = useQueryClient()
  useEffect(() => {
    const refresh = () => { for (const k of ['workspaces', 'settings', 'sign-default']) void qc.invalidateQueries({ queryKey: [k] }) }
    window.addEventListener(SIGNING_METHOD_STALE, refresh)
    return () => window.removeEventListener(SIGNING_METHOD_STALE, refresh)
  }, [qc])
}
