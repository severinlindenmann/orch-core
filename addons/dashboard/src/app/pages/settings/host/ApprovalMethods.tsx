import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
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
  const qc = useQueryClient()
  const [draft, setDraft] = useState<SignMethod[] | null>(null)
  const [method, setMethod] = useState<SignMethod | null>(null)
  const [fallback, setFallback] = useState<SignMethod | '' | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const selected = draft ?? (unrestricted ? (allowed ?? ['terminal']).filter(m => m !== 'phone' || phonePaired) : allowed) ?? ['terminal']
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
  const personal = (allowed ?? ['terminal']).filter(m => m !== 'phone')
  const readOnly = loading ? 'Reading approval settings…' : !allowed ? 'This host does not report its approval settings, so they cannot be changed here.' : null
  return <>
    <Section title="Allowed ways to approve">
      <div className="space-y-3 text-[13px]">
        {unrestricted && <p>No limit is set: every way below can be used.</p>}
        <p className="text-text-muted">Choose which methods people can use in this workspace.</p>
        {(['terminal', 'click', 'phone'] as const).map(m => <div key={m} className="flex flex-wrap items-center gap-x-2"><label className="flex items-center gap-2"><input type="checkbox" checked={selected.includes(m)} disabled={!owner || !allowed || m === 'terminal'} onChange={e => setDraft(e.target.checked ? [...selected, m] : selected.filter(x => x !== m))} />{m === 'terminal' ? 'Terminal (always)' : m === 'click' ? 'One click on this computer' : 'Phone (Face ID)'}</label>{m === 'phone' && !phonePaired && <span className="text-text-muted">no phone paired yet</span>}</div>)}
        <label className="flex items-center gap-2 text-text-muted"><input type="checkbox" disabled />Touch ID on this Mac · coming</label>
        <label className="flex items-center gap-2 text-text-muted"><input type="checkbox" disabled />Security key · later</label>
        {readOnly && <p className="text-text-muted">{readOnly}</p>}
        {!owner && <p className="text-text-muted">Only an owner changes allowed methods.</p>}
        <Button disabled={!owner || !allowed || !draft} onClick={() => ask({ title: 'Change allowed ways to approve', action: { action: 'settings_set', sign_methods: selected }, autoStart: true })}>Save allowed methods</Button>
      </div>
    </Section>
    <Section title="My settings → How I approve">
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
