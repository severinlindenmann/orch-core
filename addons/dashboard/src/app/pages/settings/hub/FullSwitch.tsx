import { useState } from 'react'
import { Action, Field, Flow } from './shared'
import { TerminalStep } from './TerminalStep'

export function FullSwitch({ ws, onClose }: { ws: string; onClose: () => void }) {
  const [card, setCard] = useState('')
  const [terminal, setTerminal] = useState(false)
  const [done, setDone] = useState(false)
  const review = <><h3 className="font-semibold">Let the hub read everything</h3>
    <div className="divide-y divide-border rounded border border-border">
      {[['What the hub sees', 'Only shared items, notes and questions', 'Every ticket, file and note'], ['Who is an admin', 'Your workspace admins', 'Workspace admins and hub admins'], ['Approvals', 'Signed locally', 'Signed on your phone or passkey']].map(([label, before, after]) =>
        <div key={label} className="grid gap-2 p-3 @md/page:grid-cols-3"><strong>{label}</strong><p><span className="text-text-muted">Before: </span>{before}</p><p><span className="text-text-muted">After: </span>{after}</p></div>)}
    </div><p className="text-warning">One way: to share only some things again, leave the hub and rejoin. What the hub has already read cannot be taken back.</p></>
  return <Flow title="Let the hub read everything" onClose={onClose}>
    <p className="text-text-muted">This is signed in a terminal on your computer, with your key there. It is the only way: the hub reading everything is not signed by One click.</p>
    {done ? <p role="status">The hub can now read everything in this workspace.</p> : terminal ? <>
      <TerminalStep ws={ws} request={{ kind: 'mode_full', card }} onStatus={s => { if (s.mode === 'full') { setDone(true); return true } return false }}>{review}</TerminalStep>
      <Action id="hub-full-back" onClick={() => setTerminal(false)}>Back</Action>
    </> : <>{review}
      <Field label="Hub card file path" value={card} onChange={setCard} testId="hub-full-card" hint="A file on the computer running this workspace." />
      <Action id="hub-full-continue" disabled={!card.trim()} onClick={() => setTerminal(true)}>Continue to the terminal</Action>
    </>}
  </Flow>
}
