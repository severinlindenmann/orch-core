import { useState } from 'react'
import { Action, Flow, Panel } from './shared'
import { TerminalStep } from './TerminalStep'

// "Try now" is left out until core has a real retry command: `orch hub unbind` is what stops the hub being owed the message.
export function NotTold({ ws }: { ws: string }) {
  const [open, setOpen] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [done, setDone] = useState(false)
  return <><Panel title="You left the hub, but the hub does not know yet" tone="border-warning/40 bg-warning-soft">
    <p>Until it is told, the hub may still show this workspace as joined. Telling it does not take back what it already read.</p>
    <p className="text-text-muted">The workspace keeps trying to tell the hub whenever it is reachable. Stopping that means the hub keeps listing this workspace as joined.</p>
    <div className="flex flex-wrap gap-2"><Action id="hub-not-told-stop" onClick={() => { setOpen(true); setConfirmed(false); setDone(false) }}>Stop telling the hub</Action></div>
  </Panel>
  {open && <Flow title="Stop telling the hub" onClose={() => setOpen(false)}>
    {done ? <p role="status">The workspace no longer lists the hub as waiting to be told.</p> : confirmed ? <TerminalStep ws={ws} request={{ kind: 'stop_telling' }} onStatus={s => { if (!s.not_told) { setDone(true); return true } return false }}>
      <p>Stop trying to tell the hub that you left.</p>
      <p>The hub will keep listing this workspace as joined. That only matters if it comes back.</p>
    </TerminalStep> : <><p>The hub will keep listing this workspace as joined. That only matters if it comes back.</p><Action id="hub-stop-telling-confirm" onClick={() => setConfirmed(true)}>Continue to Terminal</Action></>}
  </Flow>}</>
}
