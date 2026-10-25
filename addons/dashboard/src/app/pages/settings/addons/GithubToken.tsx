// #1198 C16: the GitHub token, typed here once and sealed in this browser to the signer's one-use key (purpose
// github.token). The dashboard server only relays ciphertext; the signer stores it for the GitHub add-on, the same
// place `orch addon login github` does. The typed text lives in the input only: it is read once at the click, the field
// is emptied at once, and nothing else holds it (no state, no storage).
import { Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { CustodySealOffer } from '@/api/click'
import { seal } from '@/api/sealed'
import { ApiError, type Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'

const MESSAGES: Record<string, string> = {
  'role.denied': 'Only an owner saves the GitHub token.',
  'click.contract': 'Update orch-core: its signing helper cannot take a GitHub token yet.',
  'passkey.stepup_required': 'Confirm with your fingerprint or PIN first, then try again.',
}

function sentence(e: unknown): string {
  if (e instanceof ApiError) {
    if ((e.code === 'invalid.input' || e.code === 'custody.unavailable') && e.message) return e.message.endsWith('.') ? e.message : `${e.message}.`
    return MESSAGES[e.code] ?? 'The token was not saved.'
  }
  return 'The token was not saved.'
}

const offerOk = (o: CustodySealOffer) =>
  o.seal?.pk?.length === 87 && o.seal.purposes.length === 1 && o.seal.purposes[0] === 'github.token'

export function GithubToken({ workspace }: { workspace: Workspace }) {
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ask' | 'sending' | 'saved' | 'removed'>('idle')
  const [offer, setOffer] = useState<CustodySealOffer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const field = useRef<HTMLInputElement>(null)
  const busy = useRef(false)

  const clear = () => {
    if (field.current) field.current.value = ''
    setReady(false)
  }
  useEffect(() => clear, [])

  const begin = async () => {
    if (busy.current) return
    busy.current = true
    setPhase('loading')
    try {
      const got = await api.githubSeal(workspace.id)
      if (!offerOk(got)) {
        setError('The signing helper’s offer is not what it should be. Nothing changed.')
        setPhase('idle')
      } else {
        setOffer(got)
        setPhase('ask')
      }
    } catch (e) {
      setError(sentence(e))
      setPhase('idle')
    } finally {
      busy.current = false
    }
  }

  const save = async () => {
    if (!offer || busy.current || !field.current) return
    busy.current = true
    let text: string | null = field.current.value.trim()
    clear() // emptied before anything is sealed or sent: a failure never leaves the token on screen
    setError(null)
    setPhase('sending')
    try {
      const env = await seal(offer.seal.pk, text, { purpose: 'github.token', workspace_id: workspace.id, request_id: offer.request_id, view_digest: offer.view_digest })
      text = null
      await api.githubSave(workspace.id, { request_id: offer.request_id, view_digest: offer.view_digest, sealed: { 'github.token': env } })
      setOffer(null)
      setPhase('saved')
    } catch (e) {
      text = null
      setError(sentence(e))
      busy.current = false
      await begin() // the signer's key is used up whatever went wrong: a fresh one, the field empty
      return
    }
    busy.current = false
  }

  const remove = async () => {
    if (busy.current) return
    busy.current = true
    setError(null)
    try {
      await api.githubRemove(workspace.id)
      setOffer(null)
      setPhase('removed')
    } catch (e) {
      setError(sentence(e))
    } finally {
      busy.current = false
    }
  }

  return (
    <section data-testid="github-token" aria-labelledby="github-token-heading" className="mt-3 space-y-2 border-t border-border px-4 pt-3">
      <h4 id="github-token-heading" className="text-[14px] font-semibold">GitHub token</h4>
      <p className="text-[13px] text-text-muted">Read-only access to your repositories. It stays on this computer and only the GitHub add-on gets it.</p>
      {error && <p role="alert" data-testid="github-token-error" className="text-[13px] text-danger">{error}</p>}
      {phase === 'saved' && <p role="status" data-testid="github-token-saved" className="text-[13px]">Saved. Only the GitHub add-on gets this token.</p>}
      {phase === 'removed' && <p role="status" data-testid="github-token-removed" className="text-[13px]">The token was removed.</p>}
      {(phase === 'ask' || phase === 'sending') && offer ? (
        <div className="space-y-2">
          <label className="block text-[14px] font-medium" htmlFor="github-token-input">Token</label>
          <input id="github-token-input" ref={field} type="password" autoComplete="new-password" data-1p-ignore data-lpignore="true" autoCorrect="off" autoCapitalize="off" spellCheck={false} data-testid="github-token-input" onChange={() => setReady(!!field.current?.value.trim())} className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[16px] text-text" />
          <div className="flex flex-wrap gap-2">
            <Button type="button" className="min-h-11" onClick={() => void save()} disabled={!ready || phase === 'sending'} data-testid="github-token-save">
              {phase === 'sending' ? <Loader2 className="mr-2 size-4 animate-spin" aria-hidden /> : null}Save token
            </Button>
            <Button type="button" variant="ghost" className="min-h-11" disabled={phase === 'sending'} onClick={() => { clear(); setOffer(null); setPhase('idle') }}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" className="min-h-11" onClick={() => { setError(null); void begin() }} disabled={phase === 'loading'} data-testid="github-token-start">
            {phase === 'loading' ? <Loader2 className="mr-2 size-4 animate-spin" aria-hidden /> : null}{phase === 'saved' ? 'Replace token' : 'Add token'}
          </Button>
          <Button variant="ghost" className="min-h-11" onClick={() => void remove()} data-testid="github-token-remove">Remove token</Button>
        </div>
      )}
    </section>
  )
}
