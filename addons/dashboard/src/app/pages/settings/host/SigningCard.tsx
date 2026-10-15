// "Sign in the dashboard" (host contract section 0.8.6 item 3a): the owner switches click-to-sign on and off here.
// On proves the person's CURRENT passphrase of a key that is in passphrase custody now (the one factor that someone
// who takes over this dashboard does not have); core's signer moves the device key into the key store with it and
// sets the workspace flag. A key already in the key store with signing off is switched on in the terminal: there is
// nothing to prove here. Off needs nothing (it only takes authority away) and never re-keys: moving the key back
// under a passphrase is a terminal command. The passphrase is sent once and never kept here.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import type { ClickCard, ClickSwitch } from '@/api/click'
import { signerProblem } from '@/api/refusals'
import { ApiError, type Workspace } from '@/api/types'
import { ClickRisk } from '@/components/sign/ClickSignPanel'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Section } from '../../ticket/shared'

const MESSAGES: Record<string, string> = {
  'custody.wrong_passphrase': 'That passphrase was refused. Nothing changed.',
  'custody.no_key': 'This computer holds no device key for this workspace.',
  'custody.unavailable': 'The key store is locked, refused access, or a custody change is unfinished. Check in the terminal: orch keys custody status',
  'sign.click_only': 'A sign request is waiting in a terminal. Let it expire (10 minutes), then try again.',
  'role.denied': 'Only an owner switches signing in the dashboard.',
  'click.contract': 'Update orch-core: its signing helper cannot switch signing yet.',
  'click.unavailable': 'The signing helper is not running. Try again in a moment.',
  'lock.busy': 'Something else is changing signing right now. Try again in a moment.',
}

function message(e: unknown, on: boolean, prefix: string): string {
  if (!(e instanceof ApiError)) return 'Could not reach the dashboard server. Try again.'
  if (e.code === 'rate.limited') {
    const s = Math.max(1, Math.ceil((e.retry_after_ms ?? 1000) / 1000))
    return `Too many wrong passphrases. Try again in ${s >= 120 ? `${Math.ceil(s / 60)} min` : `${s} s`}, or use the terminal.`
  }
  if (e.code === 'custody.terminal_needed') {
    return `Nothing changed. Switch it ${on ? 'on' : 'off'} in the terminal: orch host convenience ${on ? 'on' : 'off'} --workspace ${prefix}`
  }
  return MESSAGES[e.code] ?? (e.message || `Refused (${e.code}). Nothing changed.`)
}

export function SigningCard({ workspace, owner }: { workspace: Workspace; owner: boolean }) {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['click-status', workspace.id], queryFn: () => api.clickStatus(workspace.id) })
  const card: ClickCard | null = status.data ?? workspace.click ?? null
  const on = !!card && card.enabled
  const [mode, setMode] = useState<'idle' | 'on' | 'off'>('idle')
  const [pass, setPass] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setMode('idle')
    setPass('')
    setError(null)
  }
  // a key already in the key store with signing off: the dashboard has no passphrase to check (Opus H1 on #539)
  const terminalOn = !!card && !on && card.custody === 'convenience'
  const submit = async (body: ClickSwitch) => {
    setBusy(true)
    setError(null)
    try {
      const got = await api.switchClick(workspace.id, body)
      qc.setQueryData(['click-status', workspace.id], got)
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      reset()
    } catch (e) {
      setError(message(e, body.on, workspace.prefix))
      setPass('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Sign in the dashboard">
      <div data-testid="workspace-card-signing" className="space-y-3 text-[13px]">
        <ClickRisk />
        <p data-testid="signing-state">
          {on && card?.available
            ? 'On: actions are signed here with one click. "Sign in the terminal instead" stays in every dialog.'
            : on
              ? `On, but not available now.${card?.problem ? ` ${signerProblem(card.problem)}` : ''} Sign in the terminal.`
              : 'Off: every signature is made in the terminal with your passphrase.'}
        </p>
        {card?.problem === 'readers_old' && (
          <p data-testid="signing-readers-old">
            <Link to="/settings/$tab" params={{ tab: 'devices' }} className="text-brand underline underline-offset-2 hover:no-underline">
              See which devices need an update
            </Link>
          </p>
        )}
        {!owner ? (
          <p className="text-text-muted">Only an owner switches this.</p>
        ) : card && card.switchable === false ? (
          <p data-testid="signing-update" className="text-text-muted">
            This orch version cannot switch it here. Update orch-core, or use the terminal: <code>orch host convenience {on ? 'off' : 'on'} --workspace {workspace.prefix}</code>
          </p>
        ) : terminalOn ? (
          <p data-testid="signing-terminal-on" className="text-text-muted">
            Your key is already in this computer's key store, so there is no passphrase to check here. Switch it on in the terminal:{' '}
            <code>orch host convenience on --workspace {workspace.prefix}</code>
          </p>
        ) : mode === 'idle' ? (
          <div className="flex justify-end">
            {on ? (
              <Button type="button" variant="outline" onClick={() => setMode('off')}>
                Turn off
              </Button>
            ) : (
              <Button type="button" onClick={() => setMode('on')}>
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
            <dl data-testid="signing-conversion" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              <dt className="text-text-muted">Workspace</dt>
              <dd>
                {workspace.name} ({workspace.prefix})
              </dd>
              <dt className="text-text-muted">Device key</dt>
              <dd className="break-all font-mono">{card?.device ?? "this computer's key for this workspace"}</dd>
              <dt className="text-text-muted">Change</dt>
              <dd>Moves to convenience custody: from your passphrase into this computer's key store</dd>
            </dl>
            <Label htmlFor="signing-passphrase">Your passphrase for this workspace (asked once, to prove the key is yours; it is not kept)</Label>
            <Input
              id="signing-passphrase"
              type="password"
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              disabled={busy}
            />
            {error && (
              <p role="alert" data-testid="signing-error" className="text-warning">
                {error}
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
              void submit({ on: false })
            }}
          >
            <p className="text-text-muted">Your key stays in this computer's key store; nothing here can sign while signing is off.</p>
            <p data-testid="signing-move-back" className="text-text-muted">
              To move it back under a passphrase, run in the terminal: <code>orch keys custody passphrase --workspace {workspace.prefix} --in-place</code>
            </p>
            {error && (
              <p role="alert" data-testid="signing-error" className="text-warning">
                {error}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={reset} disabled={busy}>
                Cancel
              </Button>
              <Button type="submit" variant="outline" disabled={busy}>
                {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} Turn off
              </Button>
            </div>
          </form>
        )}
      </div>
    </Section>
  )
}
