import { useState } from 'react'
import { inviteHubId } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Action, Field, Flow, Panel, violet } from './shared'
import { TerminalStep } from './TerminalStep'

/** The backup way to connect: the terminal builds the command (a full join always comes here: it needs your key in a terminal). */
export function TerminalJoin({ ws, onClose, onBack, first = null }: { ws: string; onClose: () => void; onBack: () => void; first?: 'selective' | 'full' | null }) {
  const [step, setStep] = useState(1)
  const [invite, setInvite] = useState('')
  const [card, setCard] = useState('')
  const [mode, setMode] = useState<'selective' | 'full' | null>(first)
  const [done, setDone] = useState<'selective' | 'full' | null>(null)
  return <Flow title="Connect a hub in a terminal" onClose={onClose}>
    <p className="text-xs text-text-muted">Step {done ? 4 : step} of 4</p>
    {done ? <Panel title={done === 'full' ? 'The hub can read everything in this workspace' : 'Connected: only what you choose is shared'} tone={violet}>
      <p>{done === 'full' ? 'Hub admins act as admins here. Approvals use your phone or passkey.' : 'Needs-you notes and questions are always shared. You can choose more in Hub settings.'}</p>
      <Action id="hub-join-done" onClick={onClose}>Back to Hub</Action>
    </Panel> : <>
      {step === 1 && <>
        <Field label="Invite code" value={invite} onChange={setInvite} testId="hub-invite" hint="Ask your hub admin for the invite code. Paste it exactly." />
        <Field label="Hub card file path" value={card} onChange={setCard} testId="hub-card" hint="The file your hub admin gave you, on the computer that runs this workspace. For example /Users/you/acme-hub-card.json" />
        <p>The terminal checks the invite and shows the hub's fingerprint. Ask your hub admin to read you the fingerprint; if it differs, stop.</p>
        <div className="flex flex-wrap gap-2"><Action id="hub-join-back-click" onClick={onBack}>Back</Action><Action id="hub-join-continue" disabled={!invite.trim() || !card.trim()} onClick={() => setStep(2)}>Continue</Action></div>
      </>}
      {step === 2 && <>
        <h3 className="font-semibold">What should the hub be able to see?</h3>
        <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-3" role="group" aria-label="What the hub can see">
          {(['selective', 'full'] as const).map(value => <button key={value} data-testid={'hub-mode-' + value} aria-pressed={mode === value} className={'space-y-3 rounded-lg border p-4 text-left focus-visible:ring-2 focus-visible:ring-chart-4 ' + (mode === value ? violet : 'border-border')} onClick={() => setMode(value)}>
            <strong>{value === 'selective' ? 'Only what I choose' : 'Everything'}</strong>
            <p>{value === 'selective' ? 'The hub sees needs-you notes, questions and tickets you pick. You keep signing locally. Everything else stays on this computer.' : 'The hub can read every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.'}</p>
          </button>)}
        </div>
        <p>Needs-you notes and questions are always shared. You can allow everything later. Going back requires leaving and rejoining.</p>
        <div className="flex flex-wrap gap-2"><Action id="hub-join-back" onClick={() => setStep(1)}>Back</Action><Action id="hub-join-review" disabled={!mode} onClick={() => setStep(3)}>Continue</Action></div>
      </>}
      {step === 3 && mode && <>
        <TerminalStep ws={ws} request={{ kind: 'join', invite, card, mode }} onStatus={s => {
          if (s.joined && (s.mode === 'selective' || s.mode === 'full')) { setDone(s.mode); return true }
          return false
        }}>
          <h3 className="font-semibold">Review your connection</h3>
          <p data-testid="hub-invite-id">{inviteHubId(invite) ? <>Hub id in your invite: <Raw>{inviteHubId(invite) ?? ''}</Raw>. Compare it with what your hub admin tells you. It comes from the invite you typed, not from the card file.</> : 'Your invite does not name a hub id. The terminal decides whether it accepts it; ask your hub admin for an invite that does.'}</p>
          <p>{mode === 'full' ? 'The hub will read everything. Hub admins will act as admins here.' : 'The hub will see only what you share, plus needs-you notes and questions.'}</p>
          <p>The terminal checks the invite and shows the hub's fingerprint. Ask your hub admin to read you the fingerprint; if it differs, stop.</p>
        </TerminalStep>
        <Action id="hub-join-edit" onClick={() => setStep(1)}>Back to connection details</Action>
      </>}
    </>}
  </Flow>
}
