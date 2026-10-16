import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { methodLabel, type SignMethod } from '@/api/signing'
import { useSignMethods } from '@/components/sign/SignMethods'
import { ClickRisk } from '@/components/sign/ClickSignPanel'
import { Button } from '@/components/ui/button'
import { Section } from '../../ticket/shared'
import { useSettingsHandoff } from './useSettingsHandoff'

export function ApprovalMethods({ ws, owner }: { ws: string; owner: boolean }) {
  const { allowed, unrestricted, preference, loading } = useSignMethods(ws)
  const qc = useQueryClient()
  const [draft, setDraft] = useState<SignMethod[] | null>(null)
  const [method, setMethod] = useState<SignMethod | null>(null)
  const [fallback, setFallback] = useState<SignMethod | '' | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const selected = draft ?? allowed ?? ['terminal']
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
  const readOnly = loading ? 'Reading approval settings…' : !allowed ? 'This host does not provide approval settings. These controls are read-only.' : null
  return <>
    <Section title="Allowed ways to approve">
      <div className="space-y-3 text-[13px]">
        {unrestricted && <p>All ways allowed (not restricted)</p>}
        <p className="text-text-muted">Choose which methods people can use in this workspace.</p>
        {(['terminal', 'click', 'phone'] as const).map(m => <label key={m} className="flex items-center gap-2"><input type="checkbox" checked={selected.includes(m)} disabled={!owner || !allowed || m === 'terminal'} onChange={e => setDraft(e.target.checked ? [...selected, m] : selected.filter(x => x !== m))} />{m === 'terminal' ? 'Terminal (always)' : m === 'click' ? 'One click on this computer' : 'Phone (Face ID)'}</label>)}
        <label className="flex items-center gap-2 text-text-muted"><input type="checkbox" disabled />Touch ID on this Mac · coming</label>
        <label className="flex items-center gap-2 text-text-muted"><input type="checkbox" disabled />Security key · later</label>
        <ClickRisk />
        {readOnly && <p className="text-text-muted">{readOnly}</p>}
        {!owner && <p className="text-text-muted">Only an owner changes allowed methods.</p>}
        <Button disabled={!owner || !allowed || !draft} onClick={() => ask({ title: 'Change allowed ways to approve', action: { action: 'settings_set', sign_methods: selected }, autoStart: true })}>Save allowed methods</Button>
      </div>
    </Section>
    <Section title="My settings → How I approve">
      <div className="space-y-3 text-[13px]">
        <p className="text-text-muted">Your preference on this device. Choose from the methods your workspace allows.</p>
        <label className="flex items-center gap-3">Default<select className="rounded border border-border bg-bg p-2" value={primary} disabled={!allowed || !preference || busy} onChange={e => { const next = e.target.value as SignMethod; setMethod(next); if (secondary === next) setFallback('') }}>{(allowed ?? ['terminal']).map(m => <option key={m} value={m} disabled={m === 'phone'}>{methodLabel[m]}{m === 'phone' && ' · coming soon'}</option>)}</select></label>
        <label className="flex items-center gap-3">Fallback<select className="rounded border border-border bg-bg p-2" value={secondary} disabled={!allowed || !preference || busy} onChange={e => setFallback(e.target.value as SignMethod | '')}><option value="">None</option>{(allowed ?? ['terminal']).filter(m => m !== primary).map(m => <option key={m} value={m} disabled={m === 'phone'}>{methodLabel[m]}{m === 'phone' && ' · coming soon'}</option>)}</select></label>
        {(readOnly || !preference) && <p className="text-text-muted">{readOnly ?? 'This host does not provide personal approval preferences. These controls are read-only.'}</p>}
        <Button disabled={!allowed || !preference || busy || (!method && fallback === null) || primary === 'phone' || secondary === 'phone' || !allowed.includes(primary) || (!!secondary && (!allowed.includes(secondary) || secondary === primary))} onClick={() => void savePreference()}>Save my preference</Button>
        {message && <p role="status">{message}</p>}
      </div>
    </Section>
    {dialog}
  </>
}
