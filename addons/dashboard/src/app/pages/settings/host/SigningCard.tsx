// "Sign in the dashboard" (host contract section 0.8.6 item 3a): the owner switches click-to-sign on and off here.
// On proves the person's CURRENT passphrase of a key that is in passphrase custody now (the one factor that someone
// who takes over this dashboard does not have); core's signer moves the device key into the key store with it and
// sets the workspace flag. A key already in the key store with signing off is switched on in the terminal: there is
// nothing to prove here. Off (V3-D31) takes the authority away AND moves the key back under a NEW passphrase the
// person types here twice (core accepts it only while signing is on, in the same locked step). Passphrases are sent
// once and never kept here.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { ClickCard, ClickSwitch } from '@/api/click'
import { convenienceOffCommand, convenienceOnCommand, custodyPassphraseCommand } from '@/api/pairing'
import { DEV_HOST_SENTENCE, DEV_REFUSED, devRefusedSentence, signerProblem } from '@/api/refusals'
import { ApiError, type Workspace } from '@/api/types'
import { ClickRisk } from '@/components/sign/ClickSignPanel'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { CopyButton } from '../../ticket/Header'
import { Section } from '../../ticket/shared'
import { fmtDateTime } from '@/lib/time'

/** Refusals after which nothing changed: the signer said so before (or instead of) moving anything. */
/** The shortest passphrase core accepts (custody/strength.py MIN_CHARS); core still judges the strength. */
const MIN_NEW_PASSPHRASE = 14

const MESSAGES: Record<string, string> = {
  'custody.wrong_passphrase': 'That passphrase was refused. Nothing changed.',
  'custody.no_key': 'This computer holds no device key for this workspace.',
  'custody.unavailable': 'The key store is locked, refused access, or a custody change is unfinished. Check in the terminal: orch keys custody status',
  'role.denied': 'Only an owner switches signing in the dashboard.',
  'click.contract': 'Update orch-core: its signing helper cannot switch signing yet.',
  'lock.busy': 'Something else is changing signing right now. Try again in a moment.',
  'invalid.input': 'The dashboard refused that request. Nothing changed.',
}

/** A command the person copies into a terminal, or, when the workspace prefix can't be put in one safely (it comes
 * from the host), a description without a command (the same `shellWord` gate as pairing). */
function Command({ command, what }: { command: string | null; what: string }) {
  if (command === null) return <span data-testid="signing-command-unsafe">run {what} for this workspace (its prefix has characters that can&apos;t be put in a command safely)</span>
  return (
    <span className="inline-flex max-w-full items-center gap-1 align-middle">
      <code className="select-all break-all rounded bg-surface-3 px-1 font-mono text-[12px] text-text">{command}</code>
      <CopyButton text={command} label="Copy the command" />
    </span>
  )
}

interface Failure {
  text: string
  command?: { command: string | null; what: string }
}

function failure(e: unknown, on: boolean, prefix: string): Failure {
  if (!(e instanceof ApiError)) return { text: 'Could not reach the dashboard server, so the result is unknown. Reloading the card…' }
  if (e.code === 'rate.limited') {
    const s = Math.max(1, Math.ceil((e.retry_after_ms ?? 1000) / 1000))
    return { text: `Too many wrong passphrases. Try again in ${s >= 120 ? `${Math.ceil(s / 60)} min` : `${s} s`}, or use the terminal.` }
  }
  // Core D1 (#582): a dev sandbox host never switches signing on from the dashboard.
  if (e.code === DEV_REFUSED) return { text: `${devRefusedSentence(e)} Nothing changed.` }
  if (e.code === 'custody.terminal_needed') {
    return {
      text: `Nothing changed. Switch it ${on ? 'on' : 'off'} in the terminal:`,
      command: { command: on ? convenienceOnCommand(prefix) : convenienceOffCommand(prefix), what: `orch host convenience ${on ? 'on' : 'off'}` },
    }
  }
  // Core refused the new passphrase as too weak before anything changed (it names the rule, never the text).
  if (!on && e.code === 'custody.unavailable' && /passphrase/i.test(e.message ?? '') && /(at least|common|predictable)/.test(e.message ?? '')) {
    return { text: `${e.message}. Nothing changed: one-click signing is still on. Enter your new passphrase again.` }
  }
  // A timeout, a lost signer or a code this page does not know: the switch may have happened (orch-9d M2).
  return { text: MESSAGES[e.code] ?? 'The signing helper did not give a clear answer, so the result is unknown. Reloading the card…' }
}

export function SigningCard({ workspace, owner }: { workspace: Workspace; owner: boolean }) {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['click-status', workspace.id], queryFn: () => api.clickStatus(workspace.id) })
  const card: ClickCard | null = status.data ?? workspace.click ?? null
  const on = !!card && card.enabled
  const [mode, setMode] = useState<'idle' | 'on' | 'off'>('idle')
  const [pass, setPass] = useState('')
  const [pass2, setPass2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Failure | null>(null)
  const errorId = useId()
  const inFlight = useRef(false) // a second submit in the same tick, before `busy` re-renders, sends nothing

  const reset = useCallback(() => {
    setMode('idle')
    setPass('')
    setPass2('')
    setError(null)
  }, [])
  // The state the form was opened on. When the card's state changes under an open form (a "result unknown" outcome
  // whose reload found the switch did happen, or someone using the terminal), the form is stale: close it, clear its
  // message and show the new state. The passphrase goes with it (#580).
  const cardState = card ? `${card.custody}|${card.enabled}|${card.available}|${card.switchable !== false}` : 'none'
  const openedOn = useRef<string | null>(null)
  const open = (m: 'on' | 'off') => {
    openedOn.current = card ? cardState : null // opened before the first answer: that answer is not a change
    setMode(m)
  }
  useEffect(() => {
    if (mode === 'idle' || !card) return
    if (openedOn.current === null) openedOn.current = cardState
    else if (openedOn.current !== cardState) reset()
  }, [card, cardState, mode, reset])
  // a key already in the key store with signing off: the dashboard has no passphrase to check (Opus H1 on #539)
  const terminalOn = !!card && !on && card.custody === 'convenience'
  // Off with a key in the key store moves it back under a new passphrase (V3-D31); with another custody it is the flag only.
  const moveBack = !!card && card.custody === 'convenience'
  const newOk = [...pass].length >= MIN_NEW_PASSPHRASE && pass === pass2
  const submit = async (body: ClickSwitch) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    try {
      const got = await api.switchClick(workspace.id, body)
      qc.setQueryData(['click-status', workspace.id], got)
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      reset()
    } catch (e) {
      setError(failure(e, body.on, workspace.prefix))
      setPass('')
      setPass2('')
      // whatever the answer, the card shows what the signer says now (a stale form after a refusal, orch-9d M2)
      void qc.invalidateQueries({ queryKey: ['click-status', workspace.id] })
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <Section title="Sign in the dashboard">
      <div data-testid="workspace-card-signing" className="space-y-3 text-[13px]">
        <ClickRisk />
        {card?.risk_ack && <p>Risk notice acknowledged on <time dateTime={card.risk_ack}>{fmtDateTime(card.risk_ack)}</time></p>}
        <p data-testid="signing-state">
          {on && card?.available
            ? 'On: actions can be signed here with one click. Hosts with approval settings offer other allowed methods under Other ways.'
            : on
              ? `On, but not available now.${card?.problem ? ` ${signerProblem(card.problem)}` : ''} Sign in the terminal.`
              : 'Off: every signature is made in the terminal with your passphrase.'}
        </p>
        {card?.problem === 'dev_host' && (
          <p data-testid="signing-dev-host" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-danger">
            {DEV_HOST_SENTENCE}
          </p>
        )}
        {card?.problem === 'readers_old' && (
          <p data-testid="signing-readers-old">
            <Link to="/settings/$tab" params={{ tab: 'devices' }} className="text-brand underline underline-offset-2 hover:no-underline">
              See which devices need an update
            </Link>
          </p>
        )}
        {card?.problem === 'dev_host' ? null : !owner ? (
          <p className="text-text-muted">Only an owner switches this.</p>
        ) : card && card.switchable === false ? (
          <p data-testid="signing-update" className="text-text-muted">
            This orch version cannot switch it here. Update orch-core, or use the terminal:{' '}
            <Command command={on ? convenienceOffCommand(workspace.prefix) : convenienceOnCommand(workspace.prefix)} what={`orch host convenience ${on ? 'off' : 'on'}`} />
          </p>
        ) : terminalOn ? (
          <p data-testid="signing-terminal-on" className="text-text-muted">
            Your key is already in this computer's key store, so there is no passphrase to check here. Switch it on in the terminal:{' '}
            <Command command={convenienceOnCommand(workspace.prefix)} what="orch host convenience on" />
          </p>
        ) : mode === 'idle' ? (
          <div className="flex justify-end">
            {on ? (
              <Button type="button" variant="outline" onClick={() => open('off')}>
                Turn off
              </Button>
            ) : (
              <Button type="button" onClick={() => open('on')}>
                Turn on…
              </Button>
            )}
          </div>
        ) : mode === 'on' ? (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (pass) void submit({ on: true, passphrase: pass })
            }}
          >
            <dl data-testid="signing-conversion" className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
              <dt className="text-text-muted">Workspace</dt>
              <dd className="min-w-0 break-words">
                {workspace.name} ({workspace.prefix})
              </dd>
              <dt className="text-text-muted">Device key</dt>
              <dd className="min-w-0 break-all font-mono">{card?.device ?? "this computer's key for this workspace"}</dd>
              <dt className="text-text-muted">Change</dt>
              <dd className="min-w-0">Moves to convenience custody: from your passphrase into this computer's key store</dd>
            </dl>
            <Label htmlFor="signing-passphrase">Your passphrase for this workspace</Label>
            <p className="text-text-muted">Asked once, to prove the key is yours. It is not kept.</p>
            {/* The CURRENT passphrase is proven here: Chromium and Safari ignore autocomplete=off on password fields,
                so say what it is (orch-9d L2 on #578). The dashboard never asks for a new one. */}
            <Input
              id="signing-passphrase"
              type="password"
              autoComplete="current-password"
              aria-describedby={error ? errorId : undefined}
              aria-invalid={error ? true : undefined}
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              disabled={busy}
            />
            {error && (
              <p role="alert" id={errorId} data-testid="signing-error" className="text-warning">
                {error.text}
                {error.command && (
                  <>
                    {' '}
                    <Command {...error.command} />
                  </>
                )}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                Cancel
              </Button>
              <Button type="submit" disabled={busy || !pass}>
                {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn on
              </Button>
            </div>
          </form>
        ) : (
          <form
            className="space-y-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (moveBack ? newOk : true) void submit(moveBack ? { on: false, passphrase: pass } : { on: false })
            }}
          >
            {moveBack ? (
              <>
                <p data-testid="signing-off-explain">
                  Turn off one-click signing and protect your key with a new passphrase. It moves your key back from this computer's key store, and you will enter this passphrase every time you sign in the terminal.
                </p>
                <p data-testid="signing-off-copies" className="text-text-muted">
                  This does not remove existing copies of your key from backups or snapshots on this computer. orch cannot erase those copies.
                </p>
                <Label htmlFor="signing-new-passphrase">New passphrase (at least {MIN_NEW_PASSPHRASE} characters, not a common phrase)</Label>
                <Input
                  id="signing-new-passphrase"
                  type="password"
                  autoComplete="new-password"
                  aria-describedby={error ? errorId : undefined}
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  value={pass}
                  onChange={(e) => setPass(e.target.value)}
                  disabled={busy}
                />
                <Label htmlFor="signing-new-passphrase-2">New passphrase again</Label>
                <Input
                  id="signing-new-passphrase-2"
                  type="password"
                  autoComplete="new-password"
                  autoCorrect="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  value={pass2}
                  onChange={(e) => setPass2(e.target.value)}
                  disabled={busy}
                />
                {pass2 !== '' && pass !== pass2 && (
                  <p data-testid="signing-mismatch" className="text-warning">
                    Passphrases don't match. Enter the same passphrase in both fields.
                  </p>
                )}
                <p className="text-text-muted">The dashboard sends your passphrase once, to protect your key and check its strength. It does not save it.</p>
              </>
            ) : (
              <>
                <p className="text-text-muted">Your key stays in this computer's key store; nothing here can sign while signing is off.</p>
                <p data-testid="signing-move-back" className="text-text-muted">
                  To move it back under a passphrase, run in the terminal:{' '}
                  <Command command={custodyPassphraseCommand(workspace.prefix)} what="orch keys custody passphrase --in-place" />
                </p>
              </>
            )}
            {error && (
              <p role="alert" id={errorId} data-testid="signing-error" className="text-warning">
                {error.text}
                {error.command && (
                  <>
                    {' '}
                    <Command {...error.command} />
                  </>
                )}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                Cancel
              </Button>
              <Button type="submit" variant="outline" disabled={busy || (moveBack && !newOk)}>
                {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn off
              </Button>
            </div>
          </form>
        )}
      </div>
    </Section>
  )
}
