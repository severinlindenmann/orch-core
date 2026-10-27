import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { pairServed } from '@/api/pairing'
import { queries } from '@/api/queries'
import { methodLabel, type SignMethod } from '@/api/signing'
import { useSignMethods } from '@/components/sign/SignMethods'
import { Button } from '@/components/ui/button'
import { Section } from '../../ticket/shared'
import { useSettingsHandoff } from './useSettingsHandoff'

export function ApprovalMethods({ ws, owner }: { ws: string; owner: boolean }) {
  const { allowed, unrestricted, preference, loading } = useSignMethods(ws)
  // A paired phone is what makes "Phone" real here; with none, an unrestricted workspace shows it unchecked.
  const phones = useQuery({ queryKey: ['phone-available', ws], queryFn: () => api.phoneAvailable(ws), retry: false })
  const phonePaired = (phones.data?.phones ?? 0) > 0
  // One click is real only where this workspace's custody is convenience (the signer's own answer, not a label).
  const workspaces = useQuery(queries.workspaces())
  const clickSetUp = workspaces.data?.find(w => w.id === ws)?.click?.custody === 'convenience'
  const setUp = (m: SignMethod) => m === 'terminal' || (m === 'click' ? clickSetUp : phonePaired)
  const qc = useQueryClient()
  // Approving through the hub is switched on by One click setup has no box here: saving this list keeps it.
  const hostSettings = useQuery(queries.hostSettings(ws))
  const hubOn = (hostSettings.data?.sign_methods as string[] | null | undefined)?.includes('hub') === true
  const hubNote = hubOn
  const [draft, setDraft] = useState<SignMethod[] | null>(null)
  const [method, setMethod] = useState<SignMethod | null>(null)
  const [fallback, setFallback] = useState<SignMethod | '' | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  // The boxes come from the SIGNED sign_methods (all three when none is signed), and only methods that are set up count as on.
  const selected = (draft ?? allowed ?? ['terminal']).filter(setUp)
  const primary = method ?? preference?.method ?? 'terminal'
  const secondary = fallback ?? preference?.fallback ?? ''
  const { ask, dialog } = useSettingsHandoff(ws, () => {
    setDraft(null)
    void qc.invalidateQueries({ queryKey: ['settings', ws] })
    void qc.invalidateQueries({ queryKey: ['sign-default', ws] })
  })
  const savePreference = async () => {
    setBusy(true); setMessage('')
    try {
      const saved = await api.putSignDefault(ws, { method: primary, ...(secondary ? { fallback: secondary } : {}) })
      qc.setQueryData(['sign-default', ws], saved)
      setMethod(null); setFallback(null); setMessage('Preference saved.')
    } catch (e) { setMessage(e instanceof Error ? e.message : 'Could not save your preference.') }
    finally { setBusy(false) }
  }
  // Phone is not a personal default yet: it needs a paired phone for each request, so it is not offered here.
  const personal = (allowed ?? ['terminal']).filter(m => m !== 'phone' && setUp(m))
  const readOnly = loading ? 'Reading approval settings…' : !allowed ? 'This host does not report its approval settings, so they cannot be changed here.' : null
  return <>
    <Section title="Allowed ways to approve in this workspace">
      <div className="space-y-3 text-[13px]">
        {unrestricted && <p>No limit is set: every way below that is set up can be used.</p>}
        <p className="text-text-muted">Choose which methods people can use in this workspace.</p>
        {(['terminal', 'click', 'phone'] as const).filter(m => setUp(m) || selected.includes(m)).map(m => <div key={m} className="flex flex-wrap items-center gap-x-2"><label className="flex items-center gap-2"><input type="checkbox" checked={selected.includes(m)} disabled={!owner || !allowed || m === 'terminal'} onChange={e => setDraft(e.target.checked ? [...selected, m] : selected.filter(x => x !== m))} />{m === 'terminal' ? 'Terminal (always)' : m === 'click' ? 'One click on this computer' : 'Phone (Face ID)'}</label></div>)}
        {hubNote && <p className="text-text-muted" data-testid="hub-approvals-methods">Approving through your hub is on.</p>}
        {!clickSetUp && <p className="text-text-muted" data-testid="click-next-step">One click is not set up on this computer. Turn it on with the switch under Approving above, or run <code>orch setup</code> in this folder.</p>}
        {!phonePaired && <p className="text-text-muted">{pairServed(ws) ? 'Phone: no phone is paired yet.' : 'Phone: add the Phone add-on first.'}</p>}
        {readOnly && <p className="text-text-muted">{readOnly}</p>}
        {!owner && <p className="text-text-muted">Only an owner changes allowed methods.</p>}
        <Button disabled={!owner || !allowed || !draft} onClick={() => ask({ title: 'Change allowed ways to approve', action: { action: 'settings_set', sign_methods: (hubOn ? [...selected.filter(m => (m as string) !== 'hub'), 'hub'] : selected) as SignMethod[] }, autoStart: true })}>Save allowed methods</Button>
      </div>
    </Section>
    <Section title="How I approve">
      {loading ? (
        <p className="text-[13px] text-text-muted">Reading your approval settings…</p>
      ) : !allowed || !preference ? (
        <p className="text-[13px] text-text-muted">Your own way of approving is not available on this host yet. Approvals open in the terminal.</p>
      ) : (
        <div className="space-y-3 text-[13px]">
          <p className="text-text-muted">Your preference on this device. Choose from the methods your workspace allows.</p>
          <label className="flex items-center gap-3">Default<select className="rounded border border-border bg-bg p-2" value={primary} disabled={!allowed || !preference || busy} onChange={e => { const next = e.target.value as SignMethod; setMethod(next); if (secondary === next) setFallback('') }}>{personal.map(m => <option key={m} value={m}>{methodLabel[m]}</option>)}</select></label>
          <label className="flex items-center gap-3">Fallback<select className="rounded border border-border bg-bg p-2" value={secondary} disabled={!allowed || !preference || busy} onChange={e => setFallback(e.target.value as SignMethod | '')}><option value="">None</option>{personal.filter(m => m !== primary).map(m => <option key={m} value={m}>{methodLabel[m]}</option>)}</select></label>
          <Button disabled={!allowed || !preference || busy || (!method && fallback === null) || !allowed.includes(primary) || (!!secondary && (!allowed.includes(secondary) || secondary === primary))} onClick={() => void savePreference()}>Save my preference</Button>
          {message && <p role="status">{message}</p>}
        </div>
      )}
    </Section>
    {dialog}
  </>
}
