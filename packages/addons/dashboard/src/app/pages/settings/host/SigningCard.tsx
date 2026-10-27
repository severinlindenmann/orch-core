// "Sign in the dashboard" (host contract section 0.8.6 item 3a): the owner switches click-to-sign on and off here.
// On proves the person's CURRENT passphrase of a key that is in passphrase custody now (the one factor that someone
// who takes over this dashboard does not have); core's signer moves the device key into the key store with it and
// sets the workspace flag. A key already in the key store with signing off is switched on in the terminal: there is
// nothing to prove here. Off (V3-D31) takes the authority away AND moves the key back under a NEW passphrase the
// person types here twice (core accepts it only while signing is on, in the same locked step). The NEW passphrase is
// sealed in this browser to the signing helper's one-use key (sealed field, purpose custody.passphrase; #1198 C12), so
// the dashboard server relays ciphertext only; the fields are emptied before anything is sent. Passphrases are sent
// once and never kept here.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { api } from '@/api/client'
import { seal, SealedError } from '@/api/sealed'
import type { ClickCard, ClickSwitch } from '@/api/click'
import { convenienceOffCommand, convenienceOnCommand, custodyPassphraseCommand } from '@/api/pairing'
import { DEV_HOST_SENTENCE, DEV_REFUSED, devRefusedSentence, signerProblem } from '@/api/refusals'
import { ApiError, type Workspace } from '@/api/types'
import { CLICK_RISK } from '@/api/click'
import { queries } from '@/api/queries'
import { Switch } from '@/components/ui/switch'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { CopyButton } from '../../ticket/Header'
import { Section } from '../../ticket/shared'
import { SignShell, SignFooter, STICKY_FOOTER } from '@/components/sign/SignShell'

const CLICK_RISK_SHORT = 'One click signs for you from this browser on this computer; anyone using this browser, and agents running as you, could approve. You can turn it off any time.'

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
  'human.click_refused': 'A new passphrase for your key can only be set from a dashboard opened on this computer. Nothing changed.',
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
  // Full mode (joined to a hub): the host signs nothing locally, so the card says approvals go through the hub, in view.
  const hostSettings = useQuery(queries.hostSettings(workspace.id))
  const viaHub = (hostSettings.data?.sign_methods as string[] | null | undefined)?.includes('hub') === true
  const [mode, setMode] = useState<'idle' | 'on' | 'off' | 'resume'>('idle')
  const [pass, setPass] = useState('')
  const [pass2, setPass2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Failure | null>(null)
  const errorId = useId()
  const formId = useId()
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
  const open = (m: 'on' | 'off' | 'resume') => {
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
  const canFlip = owner && !!card && card.problem !== 'dev_host' && card.switchable !== false
  const newOk = [...pass].length >= MIN_NEW_PASSPHRASE && pass === pass2
  /** `body` is ready to send; for "off with a new passphrase" it is made here from `typed`, sealed to the signer's
   * one-use key. `typed` is read once; the form fields were emptied before this runs. */
  const submit = async (body: ClickSwitch, typed?: string) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    try {
      let send = body
      if (typed !== undefined) {
        const offer = await api.custodySeal(workspace.id)
        let text: string | undefined = typed
        const sealed = await seal(offer.seal.pk, text, { purpose: 'custody.passphrase', workspace_id: workspace.id, request_id: offer.request_id, view_digest: offer.view_digest })
        text = undefined
        send = { on: false, request_id: offer.request_id, view_digest: offer.view_digest, sealed: { 'custody.passphrase': sealed } }
      }
      const got = await api.switchClick(workspace.id, send)
      qc.setQueryData(['click-status', workspace.id], got)
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      reset()
    } catch (e) {
      setError(e instanceof SealedError ? { text: 'Your passphrase could not be locked in this window. Nothing changed. Enter it again.' } : failure(e, body.on, workspace.prefix))
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
    <Section title="Approving">
      <div data-testid="workspace-card-signing" className="space-y-3 text-[13px]">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1 space-y-1">
            <p id="one-click-label" className="text-[14px] font-medium">Approve with one click on this computer</p>
            <p data-testid="click-risk" className="text-text-muted">{CLICK_RISK_SHORT}</p>
          </div>
          {busy && <span role="status" className="shrink-0 text-text-muted">{mode === 'off' ? 'Turning off...' : 'Turning on...'}</span>}
          <Switch
            checked={busy ? mode !== 'off' : on}
            aria-labelledby="one-click-label"
            disabled={!canFlip || mode !== 'idle'}
            onCheckedChange={() => open(on ? 'off' : terminalOn ? 'resume' : 'on')}
          />
        </div>
        <details className="text-[12px] text-text-muted">
          <summary className="cursor-pointer">Details</summary>
          <p className="mt-1" data-testid="click-risk-full">{CLICK_RISK}</p>
        </details>
        {viaHub && <p data-testid="hub-approvals" className="font-medium text-text">Approving through your hub is on.</p>}
        <p data-testid="signing-state" className={on && card?.available && !viaHub ? "sr-only" : "text-text-muted"}>
          {viaHub
            ? 'In this workspace approvals go through your hub: you approve in the hub or on your phone, not with one click here.'
            : on && card?.available
            ? 'On: you approve here with one click.'
            : on
              ? `On, but not available now.${card?.problem ? ` ${signerProblem(card.problem)}` : ''} Approve in the terminal.`
              : 'Off: you approve in the terminal with your passphrase.'}
        </p>
        {card?.problem === 'idle' && (
          <p data-testid="signing-idle-off" className="text-text-muted">
            {card.idle_hours ? `One click switched itself off after ${card.idle_hours} hours without use.` : 'One click switched itself off after a while without use.'} Turn it on again to continue.
          </p>
        )}
        {card?.problem === 'dev_host' && (
          <p data-testid="signing-dev-host" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-danger">
            {DEV_HOST_SENTENCE}
          </p>
        )}
        {card?.problem === 'readers_old' && (
          <p data-testid="signing-readers-old">
            <Link to="/settings/$tab" params={{ tab: 'members' }} className="text-brand underline underline-offset-2 hover:no-underline">
              A device needs an update
            </Link>
          </p>
        )}
        {card?.problem === 'dev_host' ? null : !owner ? (
          <p className="text-text-muted">Only an owner switches this.</p>
        ) : card && card.switchable === false ? (
          <p data-testid="signing-update" className="text-text-muted">
            This version of orch cannot switch it here. Update orch, or use the terminal:{' '}
            <Command command={on ? convenienceOffCommand(workspace.prefix) : convenienceOnCommand(workspace.prefix)} what={`orch host convenience ${on ? 'off' : 'on'}`} />
          </p>
        ) : terminalOn && mode === 'idle' ? (
          <p data-testid="signing-terminal-on" className="text-text-muted">
            Your key is already saved on this computer, so there is no passphrase to check. Use the switch to turn it on after a short review, or in the terminal:{' '}
            <Command command={convenienceOnCommand(workspace.prefix)} what="orch host convenience on" />
          </p>
        ) : null}
        {mode !== 'idle' && (
          <SignShell
            anchor={null}
            open
            onDismiss={() => { if (!inFlight.current) reset() }}
            title={mode === 'off' ? 'Turn off one-click approval' : 'Turn on one-click approval'}
            description={mode === 'off' ? "You'll type this passphrase each time you approve." : 'Your key will be stored on this computer for one-click approval.'}
            showClose={!busy}
            onOpenAutoFocus={() => {}}
          >
            {mode === 'resume' ? (
              <form
                id={formId}
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  void submit({ on: true, resume: true })
                }}
              >
                <p data-testid="signing-resume">One click approves for you again. Your key stays where it is. It switches itself off again after {card?.idle_hours ? `${card.idle_hours} hours` : 'a while'} without use.</p>
                <p data-testid="resume-risk" className="text-text-muted">{CLICK_RISK}</p>
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
                <SignFooter><div className={STICKY_FOOTER + " flex justify-end gap-2"}>
                  <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                    Cancel
                  </Button>
                  <Button type="submit" form={formId} disabled={busy}>
                    {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn on
                  </Button>
                </div></SignFooter>
              </form>
            ) : mode === 'on' ? (
              <form
                id={formId}
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (pass) void submit({ on: true, passphrase: pass })
                }}
              >
                <details className="text-text-muted">
                  <summary className="cursor-pointer">Technical details</summary>
                  <p className="break-all font-mono">Device key: {card?.device ?? "this computer's key for this workspace"}</p>
                </details>
                <Label htmlFor="signing-passphrase">Enter the passphrase you set when you turned one click off.</Label>
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
                <SignFooter><div className={STICKY_FOOTER + " flex justify-end gap-2"}>
                  <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                    Cancel
                  </Button>
                  <Button type="submit" form={formId} disabled={busy || !pass}>
                    {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn on
                  </Button>
                </div></SignFooter>
              </form>
            ) : (
              <form
                id={formId}
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!(moveBack ? newOk : true)) return
                  const typed = pass
                  setPass('') // emptied before anything is sealed or sent: the text lives in this one closure
                  setPass2('')
                  void submit({ on: false }, moveBack ? typed : undefined)
                }}
              >
                {moveBack ? (
                  <>
                    <p data-testid="signing-off-explain">
                      Protect your key with a new passphrase.
                    </p>
                    <details className="text-text-muted">
                      <summary className="cursor-pointer">Details</summary>
                      <p data-testid="signing-off-copies">Older backups of this computer may still hold your key.</p>
                    </details>
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
                    <p className="text-text-muted">Sent safely and never saved.</p>
                  </>
                ) : (
                  <>
                    <p className="text-text-muted">Your key stays saved on this computer; nothing here can sign while signing is off.</p>
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
                <SignFooter><div className={STICKY_FOOTER + " flex flex-wrap items-center justify-end gap-2"}>
                  {moveBack && (
                    <div aria-live="polite" className="mr-auto flex flex-wrap gap-3 text-[12px]">
                      <span data-testid="signing-length-check" className={[...pass].length >= MIN_NEW_PASSPHRASE ? 'text-brand' : 'text-text-muted'}>
                        {[...pass].length >= MIN_NEW_PASSPHRASE && '✓ '}14+ characters
                      </span>
                      <span data-testid="signing-match-check" className={pass !== '' && pass === pass2 ? 'text-brand' : 'text-text-muted'}>
                        {pass !== '' && pass === pass2 && '✓ '}Both match
                      </span>
                    </div>
                  )}
                  <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                    Cancel
                  </Button>
                  <Button type="submit" form={formId} variant="outline" disabled={busy || (moveBack && !newOk)}>
                    {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn off
                  </Button>
                </div></SignFooter>
              </form>
            )}
          </SignShell>
        )}
      </div>
    </Section>
  )
}
