import { useState } from 'react'
import { Action, Flow, caveat } from './shared'
import { InfoDrawer } from '@/components/InfoDrawer'
import { TerminalStep } from './TerminalStep'
import { Raw } from '@/components/sign/visible'

export function StopSharing({ ws, ticket, onClose }: { ws: string; ticket: string; onClose: () => void }) {
  const [terminal, setTerminal] = useState(false)
  const review = <><p>The hub will be asked to delete its copy of this ticket. This does not remove rules that may share it again.</p>
    <p className="text-danger">{caveat}</p>
    <p>Deletion is not confirmed here. After you run the command, the request may remain pending until the hub is reachable.</p></>
  return <Flow title="Stop sharing this ticket" onClose={onClose}>
    {terminal ? <TerminalStep ws={ws} request={{ kind: 'share_revoke', ref: ticket }}>{review}</TerminalStep> : <>{review}
      <InfoDrawer testId="hub-revoke-details"><Raw>{ticket}</Raw></InfoDrawer>
      <Action id="hub-revoke-continue" onClick={() => setTerminal(true)}>Continue to the terminal</Action>
    </>}
  </Flow>
}
