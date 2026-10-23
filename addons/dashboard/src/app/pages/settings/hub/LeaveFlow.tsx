import { useState } from 'react'
import { Action, Flow, caveat } from './shared'
import { TerminalStep } from './TerminalStep'

export function LeaveFlow({ ws, hubDown = false, onClose }: { ws: string; hubDown?: boolean; onClose: () => void }) {
  const [terminal, setTerminal] = useState(false)
  const [done, setDone] = useState<string>()
  const consequences = <div className="space-y-2">
    <p>Your tickets, agents and keys stay on this computer. You can keep working locally.</p>
    <p>Your phone, publishing and handoffs through this hub stop working. Hub members lose access through the hub.</p>
    <p className="text-danger">{caveat} The hub is asked to delete its copies.</p>
    {hubDown && <p className="text-warning">Your Terminal asks you to sign and may ask you to unlock your key. The hub will not be told until it is reachable. It may still list this workspace and keep copies of what it read.</p>}
  </div>
  return <Flow title={hubDown ? 'Leave without the hub' : 'Leave the hub'} onClose={onClose}>
    {done ? <p role="status">{done}</p> : terminal ? <TerminalStep ws={ws} request={{ kind: hubDown ? 'leave_hub_down' : 'leave' }} onStatus={s => {
      if (s.mode === 'standalone') { setDone(s.not_told ? 'You left the hub, but the hub does not know yet.' : 'You left the hub. This workspace is Local only.'); return true } return false
    }}>{consequences}</TerminalStep> : <>{consequences}<Action id="hub-leave-continue" onClick={() => setTerminal(true)}>Continue to Terminal</Action></>}
  </Flow>
}
