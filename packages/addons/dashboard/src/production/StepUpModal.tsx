import { useEffect, useRef, useState } from 'react'
import { getToken } from '../api/bootstrap'
import { onStepUp, settleStepUp, type StepUpInfo } from '../api/stepUp'

/**
 * "Confirm it's you on this computer." The page was opened from the hub and holds a read-mostly session; the first
 * change asks here. A browser that signed in on this computer before lands with a full session from the start (its key
 * proves itself before the hub link is used, bootstrap); this is the fresh-browser case. There is no way up from a
 * read-mostly session itself: the person opens the dashboard once from the terminal, this tab adopts the new token
 * from that launch link, and Try again runs the action they started. A session that came from another computer is
 * told where the change can be made.
 */
export function StepUpModal() {
  const [info, setInfo] = useState<StepUpInfo | null>(null)
  const [failed, setFailed] = useState(false)
  // The session the refusal came from: if a launch link was opened meanwhile, this tab already holds another one.
  const refused = useRef<string | null>(null)
  useEffect(() => onStepUp((i) => { refused.current = i ? getToken() : null; setInfo(i); setFailed(false) }), [])
  if (!info) return null
  const here = info.where === 'this_computer'
  const retry = () => {
    const token = getToken()
    if (token !== refused.current) { settleStepUp(true); return } // a launch link replaced the session: run the action
    setFailed(true)
  }
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="stepup-title" data-testid="stepup-modal" className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4">
      <div className="w-full max-w-md space-y-3 rounded-lg border border-border bg-surface-1 p-5 text-[13px] shadow-lg">
        <h2 id="stepup-title" className="text-[15px] font-semibold">{here ? "Confirm it's you on this computer" : 'Make this change on the workspace computer'}</h2>
        <p>{info.message}</p>
        {here && (
          <>
            <p className="text-text-muted">You came here from your hub, which can only look. To change things, open the dashboard once from the terminal on this computer: run <code>orch dashboard</code> and open the link it prints in this browser. Then come back here.</p>
            {failed && <p role="alert" className="text-danger">This browser is not confirmed yet. Open the dashboard from the terminal first, then try again.</p>}
          </>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" className="rounded border border-border px-3 py-1.5" onClick={() => settleStepUp(false)}>{here ? 'Not now' : 'Close'}</button>
          {here && <button type="button" data-testid="stepup-retry" className="rounded bg-accent px-3 py-1.5 text-accent-fg" onClick={retry}>Try again</button>}
        </div>
      </div>
    </div>
  )
}
