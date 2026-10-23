import { useState } from 'react'
import { Action, Flow, Panel } from './shared'
import { TerminalStep } from './TerminalStep'

export function NotTold({ ws }: { ws: string }) {
  const [action, setAction] = useState<'try' | 'stop'>()
  const [confirmed, setConfirmed] = useState(false)
  const [done, setDone] = useState(false)
  return <><Panel title="You left the hub, but the hub does not know yet" tone="border-warning/40 bg-warning-soft">
    <p>Until it is told, the hub may still show this workspace as joined. Telling it does not take back what it already read.</p>
    <div className="flex flex-wrap gap-2">
      <Action id="hub-not-told-try" onClick={() => { setAction('try'); setConfirmed(true); setDone(false) }}>Try now</Action>
      <Action id="hub-not-told-stop" onClick={() => { setAction('stop'); setConfirmed(false); setDone(false) }}>Stop telling</Action>
    </div>
  </Panel>
  {action && <Flow title={action === 'try' ? 'Try now' : 'Stop telling the hub'} onClose={() => setAction(undefined)}>
    {done ? <p role="status">The workspace no longer lists the hub as waiting to be told.</p> : confirmed ? <TerminalStep ws={ws} request={{ kind: 'stop_telling' }} onStatus={s => { if (!s.not_told) { setDone(true); return true } return false }}>
      <p>{action === 'try' ? 'Review the pending hub connection in Terminal.' : 'Stop trying to tell the hub you left.'}</p>
      <p>The hub will keep listing this workspace as joined. That only matters if it comes back.</p>
    </TerminalStep> : <><p>The hub will keep listing this workspace as joined. That only matters if it comes back.</p><Action id="hub-stop-telling-confirm" onClick={() => setConfirmed(true)}>Continue to Terminal</Action></>}
  </Flow>}</>
}
