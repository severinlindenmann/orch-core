// #1198 C13: "Replace this device's key". The signer shows what is replaced (the key of this computer, old and new), then
// the recovery code and a passphrase for the new key are typed once here and sealed in this browser to the signer's
// one-use key (purposes device.recovery_code and custody.passphrase). The dashboard server only relays ciphertext. The
// typed text lives in the input elements only: it is read once at the click, the fields are emptied at once, and nothing
// else holds it (no state, no storage). Core refuses unless the code is the person's own.
import { KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { rotateOfferOk, type RotateOffer } from '@/api/click'
import { seal } from '@/api/sealed'
import { ApiError, type Workspace } from '@/api/types'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Mono } from '../../ticket/shared'

/** The shortest passphrase core accepts (custody/strength.py MIN_CHARS); core still judges the strength. */
const MIN_NEW_PASSPHRASE = 14

const MESSAGES: Record<string, string> = {
  'human.click_refused': 'Replacing this computer’s key needs a dashboard opened on this computer, and a recovery code that is yours. Nothing changed.',
  'role.denied': 'Only an owner replaces a device key.',
  'click.contract': 'Update orch-core: its signing helper cannot replace a key yet.',
  'custody.no_key': 'There is no device key of yours on this computer.',
}

function sentence(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === 'human.click_refused' && /recovery code/i.test(e.message ?? '')) return `${e.message}.`.replace(/\.\.$/, '.')
    if ((e.code === 'invalid.input' || e.code === 'custody.unavailable') && e.message) return e.message.endsWith('.') ? e.message : `${e.message}.`
    if (e.code === 'rate.limited') return 'Too many wrong recovery codes. Wait a few minutes and try again.'
    return MESSAGES[e.code] ?? 'The key could not be replaced. Nothing changed.'
  }
  return 'The key could not be replaced. Nothing changed.'
}

export function ReplaceKey({ workspace, owner = true }: { workspace: Workspace; owner?: boolean }) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<'idle' | 'loading' | 'review' | 'sending' | 'done'>('idle')
  const [offer, setOffer] = useState<RotateOffer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const code = useRef<HTMLInputElement>(null)
  const pw = useRef<HTMLInputElement>(null)
  const pw2 = useRef<HTMLInputElement>(null)
  const busy = useRef(false)

  const clear = () => {
    for (const r of [code, pw, pw2]) if (r.current) r.current.value = ''
    setReady(false)
  }
  useEffect(() => clear, [])

  const begin = async () => {
    if (busy.current) return
    busy.current = true
    setPhase('loading')
    try {
      const got = await api.rotateSeal(workspace.id)
      if ('done' in got) {
        void qc.invalidateQueries({ queryKey: ['click-status', workspace.id] })
        setPhase('done')
      } else if (!rotateOfferOk(got, workspace.id)) {
        setError('The signing helper’s review is not what it should be. Nothing changed.')
        setPhase('idle')
      } else {
        setOffer(got)
        setPhase('review')
      }
    } catch (e) {
      setError(sentence(e))
      setPhase('idle')
    } finally {
      busy.current = false
    }
  }

  const check = () => {
    const a = pw.current?.value ?? ''
    setReady(!!code.current?.value.trim() && [...a].length >= MIN_NEW_PASSPHRASE && a === (pw2.current?.value ?? ''))
  }

  const replace = async () => {
    if (!offer || busy.current || !code.current || !pw.current) return
    busy.current = true
    let words: string | null = code.current.value.trim().split(/\s+/).join(' ')
    let fresh: string | null = pw.current.value
    clear() // emptied before anything is sealed or sent: a failure never leaves a secret on screen
    setError(null)
    setPhase('sending')
    try {
      const ctx = { workspace_id: workspace.id, request_id: offer.request_id, view_digest: offer.view_digest }
      const a = await seal(offer.view.seal.pk, words, { purpose: 'device.recovery_code', ...ctx })
      const b = await seal(offer.view.seal.pk, fresh, { purpose: 'custody.passphrase', ...ctx })
      words = fresh = null
      await api.rotateKey(workspace.id, { request_id: offer.request_id, view_digest: offer.view_digest, sealed: { 'device.recovery_code': a, 'custody.passphrase': b } })
      void qc.invalidateQueries({ queryKey: ['click-status', workspace.id] })
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      setOffer(null)
      setPhase('done')
    } catch (e) {
      words = fresh = null
      setError(sentence(e))
      busy.current = false
      await begin() // the signer's key is used up whatever went wrong: a new review, the fields empty
      return
    }
    busy.current = false
  }

  if (!owner) return null
  return (
    <section data-testid="replace-key" aria-labelledby="replace-key-heading" className="max-w-xl space-y-3 pb-4">
      <h2 id="replace-key-heading" className="text-base font-semibold">This computer&apos;s key</h2>
      {phase === 'done' ? (
        <p role="status" data-testid="replace-key-done" className="text-[13px]">Done. This computer has a new key; the old one no longer signs.</p>
      ) : (
        <>
          <p className="text-[13px] text-text-muted">If a copy of this computer&apos;s key may have been taken, replace it with a new one. You need your recovery code.</p>
          {error && <p role="alert" data-testid="replace-key-error" className="text-[13px] text-danger">{error}</p>}
          {(phase === 'idle' || phase === 'loading') && (
            <Button variant="outline" onClick={() => void begin()} disabled={phase === 'loading'} data-testid="replace-key-start">
              {phase === 'loading' ? <Loader2 className="mr-2 size-4 animate-spin" aria-hidden /> : null}Replace this device&apos;s key
            </Button>
          )}
          {(phase === 'review' || phase === 'sending') && offer && (
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault()
                if (ready) void replace()
              }}
            >
              <div data-testid="replace-key-review" className="space-y-1 text-[14px]">
                {offer.view.lines.slice(0, 2).map((l) => <p key={l}><Inline>{l}</Inline></p>)}
                <details>
                  <summary className="min-h-11 cursor-pointer py-2 text-text-muted">Technical details</summary>
                  <p>Old key: <Mono className="break-all"><Inline>{offer.view.old}</Inline></Mono></p>
                  <p>New key: <Mono className="break-all"><Inline>{offer.view.new}</Inline></Mono></p>
                </details>
              </div>
              <div className="space-y-1.5 pt-2">
              <label className="block text-[14px] font-medium" htmlFor="replace-key-code"><KeyRound className="mr-1 inline size-4" aria-hidden />Recovery code</label>
              <p className="text-[13px] text-text-muted">The 24 words from setup.</p>
              <input id="replace-key-code" ref={code} type="password" autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} data-testid="replace-key-code" onChange={check} className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[16px] text-text" />
              </div>
              <div className="space-y-1.5 pt-2">
              <label className="block text-[14px] font-medium" htmlFor="replace-key-pw">New passphrase</label>
              <p className="text-[13px] text-text-muted">At least {MIN_NEW_PASSPHRASE} characters. You enter it whenever you sign in a terminal.</p>
              <input id="replace-key-pw" ref={pw} type="password" autoComplete="new-password" autoCorrect="off" autoCapitalize="off" spellCheck={false} data-testid="replace-key-pw" onChange={check} className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[16px] text-text" />
              </div>
              <div className="space-y-1.5 pt-2">
              <label className="block text-[14px] font-medium" htmlFor="replace-key-pw2">Repeat new passphrase</label>
              <input id="replace-key-pw2" ref={pw2} type="password" autoComplete="new-password" autoCorrect="off" autoCapitalize="off" spellCheck={false} data-testid="replace-key-pw2" onChange={check} className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[16px] text-text" />
              </div>
              <div className="flex flex-wrap gap-2 pt-2">
                <Button type="submit" className="min-h-11" disabled={!ready || phase === 'sending'} data-testid="replace-key-go">
                  {phase === 'sending' ? <Loader2 className="mr-2 size-4 animate-spin" aria-hidden /> : null}Replace key
                </Button>
                <Button type="button" variant="ghost" className="min-h-11" disabled={phase === 'sending'} onClick={() => { clear(); setOffer(null); setPhase('idle') }}>Cancel</Button>
              </div>
            </form>
          )}
        </>
      )}
    </section>
  )
}
