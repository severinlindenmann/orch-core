// #1198 C11: connect a hub with a plain click. The person pastes (or loads) the hub card and the invite code their hub
// admin gave them, sees the hub the card names, and signs with one click and their backup key (HubStep). The signed join
// only records the join: the host then tells the hub (hub.connect, with the invite), and only then does the page say
// "Connected". Only "Only what I choose" works this way:
// letting the hub read everything needs the key in a terminal, so that choice, and the whole backup way, goes to TerminalJoin.
import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { hubCardId } from '@/api/hub'
import { newIdem } from '@/api/runs'
import { Raw } from '@/components/sign/visible'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, StrictNeeds, caveat, useFullAllowed, useHubRefresh, useOneClick } from './shared'
import { ModeChoice } from './ModeChoice'
import { TerminalJoin } from './TerminalJoin'

const MAX_CARD = 16 * 1024

export function JoinFlow({ ws, onClose }: { ws: string; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const [route, setRoute] = useState<'click' | 'terminal'>('click')
  const [first, setFirst] = useState<'selective' | 'full' | null>(null)
  const [step, setStep] = useState<'card' | 'mode' | 'sign'>('card')
  const [text, setText] = useState('')
  const [invite, setInvite] = useState('')
  const [signed, setSigned] = useState(false)
  const fullAllowed = useFullAllowed(ws)
  const [mode, setMode] = useState<'selective' | 'full' | null>(null)
  const [fileError, setFileError] = useState('')
  const file = useRef<HTMLInputElement>(null)
  const card = text.trim()
  const hub = hubCardId(card)
  const tooBig = card.length > MAX_CARD
  const code = invite.trim()

  if (route === 'terminal') return <TerminalJoin ws={ws} first={first} onClose={onClose} onBack={() => setRoute('click')} />

  const load = async (f: File | undefined) => {
    setFileError('')
    if (!f) return
    if (f.size > MAX_CARD) { setFileError('That file is too big to be a hub card.'); return }
    try { setText(await f.text()) } catch { setFileError('Could not read that file. Paste the card instead.') }
  }
  const terminal = (m: 'selective' | 'full' | null) => { setFirst(m); setRoute('terminal') }
  return <Flow title="Connect a hub" onClose={onClose} finish="here">
    <p className="text-xs text-text-muted">Step {step === 'card' ? 1 : step === 'mode' ? 2 : 3} of 3</p>
    {step === 'card' && <>
      <div className="space-y-1">
        <label htmlFor="hub-card-text" className="block text-sm font-medium">Hub card</label>
        <textarea id="hub-card-text" data-testid="hub-card-text" value={text} onChange={e => setText(e.target.value)} rows={6} spellCheck={false} aria-describedby="hub-card-hint"
          className="min-h-24 w-full rounded-md border border-border bg-bg p-2 font-mono text-xs" />
        <p id="hub-card-hint" className="text-xs text-text-muted">Your hub admin gives you the card as a file. Open it and paste what is inside, or load the file.</p>
        <input ref={file} type="file" accept=".json,application/json,text/plain" className="sr-only" tabIndex={-1} data-testid="hub-card-file" onChange={e => { void load(e.currentTarget.files?.[0]); e.currentTarget.value = '' }} />
        <Action id="hub-card-load" onClick={() => file.current?.click()}>Load the card file</Action>
        {fileError && <p role="alert" className="text-danger">{fileError}</p>}
      </div>
      <div className="space-y-1">
        <label htmlFor="hub-invite-text" className="block text-sm font-medium">Invite code (from your hub admin)</label>
        <input id="hub-invite-text" data-testid="hub-invite-text" value={invite} onChange={e => setInvite(e.target.value)} autoComplete="off" spellCheck={false} aria-describedby="hub-invite-hint"
          className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-sm" />
        <p id="hub-invite-hint" className="text-xs text-text-muted">The hub checks this code when it hears from your workspace. Paste it exactly.</p>
      </div>
      {card && (hub && !tooBig
        ? <p data-testid="hub-card-id">This card is for the hub <Raw>{hub.short}</Raw>. Ask your hub admin to read you this number; if it differs, stop.</p>
        : <p role="alert" data-testid="hub-card-bad" className="text-danger">This does not look like a hub card. Paste everything from the file, from the first brace to the last.</p>)}
      <Action id="hub-join-continue" disabled={!hub || tooBig || code.length < 4} onClick={() => setStep('mode')}>Continue</Action>
      <p className="text-xs text-text-muted">Prefer a terminal? <button type="button" data-testid="hub-join-terminal" className="underline" onClick={() => terminal(null)}>Do it in a terminal</button></p>
    </>}
    {step === 'mode' && <>
      <ModeChoice mode={mode} onChange={setMode} />
      {mode === 'full' && (fullAllowed
        ? <p data-testid="hub-full-terminal" className="rounded border border-border p-3">Letting the hub read everything is signed in a terminal on your computer, with your key there. <button type="button" className="underline" data-testid="hub-full-to-terminal" onClick={() => terminal('full')}>Do it in a terminal</button>, or choose “Only what I choose”.</p>
        : <div data-testid="hub-full-terminal" className="rounded border border-border p-3"><StrictNeeds testId="hub-full-needs" /><p>Or choose “Only what I choose”.</p></div>)}
      <div className="flex flex-wrap gap-2"><Action id="hub-join-back" onClick={() => setStep('card')}>Back</Action><Action id="hub-join-review" disabled={mode !== 'selective'} onClick={() => setStep('sign')}>Continue</Action></div>
    </>}
    {step === 'sign' && hub && <>
      <p data-testid="hub-sign-for">You are connecting this workspace to the hub <Raw>{hub.short}</Raw>. The hub will see only what you share, plus notes that need your attention and full questions. {caveat}</p>
      {oneClick !== 'on'
        ? <NoOneClick state={oneClick} what="Connecting"><Action id="hub-join-terminal-instead" onClick={() => terminal('selective')}>Do it in a terminal</Action></NoOneClick>
        : signed ? <ConnectStage ws={ws} invite={code} card={card} onClose={onClose} />
        : <HubStep ws={ws} action={{ action: 'hub_join', hub_card: card, hub_id: hub.id }} onClose={onClose} onChanged={() => { refresh(); setSigned(true) }} />}
      {!signed && <Action id="hub-join-edit" onClick={() => setStep('card')}>Back to the hub card</Action>}
    </>}
  </Flow>
}

/** The signed join is on the log; now the host tells the hub. "Connected" only when that worked. */
function ConnectStage({ ws, invite, card, onClose }: { ws: string; invite: string; card: string; onClose: () => void }) {
  const refresh = useHubRefresh(ws)
  const [state, setState] = useState<'idle' | 'working' | 'done' | 'failed'>('idle')
  const [why, setWhy] = useState('')
  const started = useRef(false)
  const run = () => {
    setState('working'); setWhy('')
    api.connectHub(ws, { idem: newIdem(), invite, hub_card: card }).then(
      () => { setState('done'); refresh() },
      e => { setWhy(e instanceof ApiError && e.message ? e.message : 'The hub did not answer.'); setState('failed') },
    )
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-3" data-testid="hub-connect-stage">
    {state === 'done' && <><p role="status" data-testid="hub-connected">Connected. The hub sees only what you choose to share, plus notes that need your attention and full questions.</p><div className="flex justify-end"><Action id="hub-connect-close" onClick={onClose}>Close</Action></div></>}
    {(state === 'idle' || state === 'working') && <p role="status">Signed. Telling the hub…</p>}
    {state === 'failed' && <div role="alert" className="space-y-2" data-testid="hub-connect-failed">
      <p className="text-danger">Your join is signed, but the hub did not accept it, so you are not connected yet. {why}</p>
      <p>Try again. If it keeps failing, leave the hub and join again with a new invite code.</p>
      <div className="flex flex-wrap gap-2"><Action id="hub-connect-retry" onClick={run}>Try again</Action><Action id="hub-connect-close" onClick={onClose}>Close</Action></div>
    </div>}
  </div>
}
