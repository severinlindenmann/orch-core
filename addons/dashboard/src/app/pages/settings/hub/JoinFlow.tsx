// #1198 C11: connect a hub with a plain click. The person pastes (or loads) the hub card their hub admin gave them, sees
// the hub it names, and signs with one click and their backup key (HubStep). Only "Only what I choose" works this way:
// letting the hub read everything needs the key in a terminal, so that choice, and the whole backup way, goes to TerminalJoin.
import { useRef, useState } from 'react'
import { hubCardId } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick, violet } from './shared'
import { TerminalJoin } from './TerminalJoin'

const MAX_CARD = 16 * 1024

export function JoinFlow({ ws, onClose }: { ws: string; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const [route, setRoute] = useState<'click' | 'terminal'>('click')
  const [first, setFirst] = useState<'selective' | 'full' | null>(null)
  const [step, setStep] = useState<'card' | 'mode' | 'sign'>('card')
  const [text, setText] = useState('')
  const [mode, setMode] = useState<'selective' | 'full' | null>(null)
  const [fileError, setFileError] = useState('')
  const file = useRef<HTMLInputElement>(null)
  const card = text.trim()
  const hub = hubCardId(card)
  const tooBig = card.length > MAX_CARD

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
      {card && (hub && !tooBig
        ? <p data-testid="hub-card-id">This card is for the hub <Raw>{hub.short}</Raw>. Ask your hub admin to read you this number; if it differs, stop.</p>
        : <p role="alert" data-testid="hub-card-bad" className="text-danger">This does not look like a hub card. Paste everything from the file, from the first brace to the last.</p>)}
      <Action id="hub-join-continue" disabled={!hub || tooBig} onClick={() => setStep('mode')}>Continue</Action>
      <p className="text-xs text-text-muted">Prefer a terminal? <button type="button" data-testid="hub-join-terminal" className="underline" onClick={() => terminal(null)}>Do it in a terminal</button></p>
    </>}
    {step === 'mode' && <>
      <h3 className="font-semibold">What should the hub be able to see?</h3>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-3" role="group" aria-label="What the hub can see">
        {(['selective', 'full'] as const).map(value => <button key={value} data-testid={'hub-mode-' + value} aria-pressed={mode === value} className={'space-y-3 rounded-lg border p-4 text-left focus-visible:ring-2 focus-visible:ring-chart-4 ' + (mode === value ? violet : 'border-border')} onClick={() => setMode(value)}>
          <strong>{value === 'selective' ? 'Only what I choose' : 'Everything'}</strong>
          <p>{value === 'selective' ? 'The hub sees the tickets you pick. Always shared: notes that need your attention, and full questions. You keep signing locally. Everything else stays on this computer.' : 'The hub can read every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.'}</p>
        </button>)}
      </div>
      <p>Notes that need your attention and full questions are always shared. You can allow everything later. Going back requires leaving and rejoining.</p>
      {mode === 'full' && <p data-testid="hub-full-terminal" className="rounded border border-border p-3">Letting the hub read everything cannot be done here: it is refused while One click is on, and otherwise needs your key in a terminal. <button type="button" className="underline" data-testid="hub-full-to-terminal" onClick={() => terminal('full')}>Do it in a terminal</button>, or choose “Only what I choose”.</p>}
      <div className="flex flex-wrap gap-2"><Action id="hub-join-back" onClick={() => setStep('card')}>Back</Action><Action id="hub-join-review" disabled={mode !== 'selective'} onClick={() => setStep('sign')}>Continue</Action></div>
    </>}
    {step === 'sign' && hub && <>
      <p data-testid="hub-sign-for">You are connecting this workspace to the hub <Raw>{hub.short}</Raw>. The hub will see only what you share, plus notes that need your attention and full questions. {caveat}</p>
      {oneClick === 'on'
        ? <HubStep ws={ws} action={{ action: 'hub_join', hub_card: card, hub_id: hub.id }} onClose={onClose} onChanged={refresh} doneText="Connected. The hub sees only what you choose to share, plus notes that need your attention and full questions." />
        : <NoOneClick state={oneClick} what="Connecting"><Action id="hub-join-terminal-instead" onClick={() => terminal('selective')}>Do it in a terminal</Action></NoOneClick>}
      <Action id="hub-join-edit" onClick={() => setStep('card')}>Back to the hub card</Action>
    </>}
  </Flow>
}
