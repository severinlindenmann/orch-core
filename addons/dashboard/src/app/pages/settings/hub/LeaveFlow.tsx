import { useState } from 'react'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick } from './shared'
import { TerminalStep } from './TerminalStep'
import { HubStep } from '../host/HubStep'

// #1198 C11: leaving is one click with the backup key (HubStep) while One click is on. The terminal is the backup way,
// and the only way when the hub cannot be reached (`hubDown`) or One click is off.
export function LeaveFlow({ ws, hubDown = false, onClose }: { ws: string; hubDown?: boolean; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const [terminal, setTerminal] = useState(hubDown)
  const [done, setDone] = useState<string>()
  const consequences = <div className="space-y-2">
    <p>Your tickets, agents and keys stay on this computer. You can keep working locally.</p>
    <p>Your phone, publishing and handoffs through this hub stop working. Hub members lose access through the hub.</p>
    <p className="text-danger">{caveat} The hub is asked to delete its copies.</p>
    {hubDown && <p className="text-warning">Your terminal asks you to sign and may ask you to unlock your key. The hub will not be told until it is reachable. It may still list this workspace and keep copies of what it read.</p>}
  </div>
  const here = !terminal && oneClick === 'on'
  return <Flow title={hubDown ? 'Leave without the hub' : 'Leave the hub'} onClose={onClose} finish={here ? 'here' : 'terminal'}>
    {done ? <p role="status">{done}</p> : terminal ? <>
      <TerminalStep ws={ws} request={{ kind: hubDown ? 'leave_hub_down' : 'leave' }} onStatus={s => {
        if (s.mode === 'standalone') { setDone(s.not_told ? 'You left the hub, but the hub does not know yet.' : 'You left the hub. This workspace is Local only.'); return true } return false
      }}>{consequences}</TerminalStep>
      {!hubDown && <Action id="hub-leave-back" onClick={() => setTerminal(false)}>Back</Action>}
    </> : <>
      {consequences}
      {here
        ? <HubStep ws={ws} action={{ action: 'hub_leave' }} onClose={onClose} onChanged={refresh} doneText="You left the hub. This workspace works on its own now." />
        : <NoOneClick state={oneClick} what="Leaving" />}
      <p className="text-xs text-text-muted">{here ? 'Prefer a terminal? ' : ''}<button type="button" data-testid="hub-leave-terminal" className="underline" onClick={() => setTerminal(true)}>Do it in a terminal</button></p>
    </>}
  </Flow>
}
