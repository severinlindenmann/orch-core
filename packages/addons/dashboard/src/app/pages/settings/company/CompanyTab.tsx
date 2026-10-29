import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { Workspace } from '@/api/types'
import { hubServed } from '@/api/hub'
import { orgQueries, STATUS_WORD, type OrgKind, type OrgRow } from '@/api/org'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { Action, Panel } from '../hub/shared'
import { ItemSheet } from './ItemSheet'

// The owner's Company tab (M3 H-5): which skills and tools the company hub offers, and a plain On/Off for each. Everything
// a hub wrote is shown as plain text (see ItemSheet). The hub is optional: no hub, no tab; hub offline, what was saved.

function Row({ kind, row, busy, onOpen, onOff }: { kind: OrgKind; row: OrgRow; busy: boolean; onOpen: () => void; onOff: () => void }) {
  const isOn = row.status === 'on' || row.status === 'needs_isolation'
  return <li className="flex items-center gap-3 py-2" data-testid={`company-row-${kind}-${row.name}`}>
    <button type="button" className="min-w-0 flex-1 text-left hover:underline" onClick={onOpen} data-testid={`company-open-${row.name}`}>
      <span className="font-medium">{row.name}</span>
      <span className={'ml-2 text-xs ' + (row.status === 'on' ? 'text-success' : row.status === 'off' ? 'text-text-muted' : 'text-warning')} data-testid={`company-status-${row.name}`}>{STATUS_WORD[row.status]}</span>
    </button>
    {isOn
      ? <Action id={`company-off-${row.name}`} disabled={busy} onClick={onOff}>Turn off</Action>
      : <Action id={`company-on-${row.name}`} onClick={onOpen}>Turn on</Action>}
  </li>
}

function syncMessage(e: unknown): string {
  const code = e instanceof ApiError ? e.code : ''
  if (code === 'hub.not_joined') return 'This workspace is not connected to a hub.'
  return 'The hub could not be reached. Showing what was saved here.'
}

export function CompanyTab({ workspace, isOwner }: { workspace: Workspace; isOwner: boolean }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const offers = useQuery({ ...orgQueries.offers(ws), enabled: hubServed(ws) && isOwner })
  const [open, setOpen] = useState<{ kind: OrgKind; name: string }>()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const refresh = () => qc.invalidateQueries({ queryKey: ['org', ws] })
  if (!hubServed(ws)) return <Panel title="Company"><p>This host does not serve a hub yet.</p></Panel>
  if (!isOwner) return <Panel title="Company"><p data-testid="company-owner-only">Only the workspace owner decides which company skills and tools are on.</p></Panel>
  if (offers.isPending) return <Panel title="Company"><p role="status">Loading what your hub offers…</p></Panel>
  if (offers.isError) return <Panel title="Company"><p role="alert">Could not load what your hub offers.</p><Action id="company-retry" onClick={() => void offers.refetch()}>Try again</Action></Panel>
  const o = offers.data
  if (!o.available) return <Panel title="Company"><p>This host cannot show company skills and tools yet. Update orch to use them.</p></Panel>

  const sync = () => {
    setBusy(true)
    setNote('')
    api.syncOrg(ws).then(
      () => { setNote('Up to date with your hub.'); void refresh() },
      (e) => setNote(syncMessage(e)),
    ).finally(() => setBusy(false))
  }
  const turnOff = (kind: OrgKind, name: string) => {
    setBusy(true)
    setNote('')
    api.declineOrgItem(ws, kind, name).then(
      () => void refresh(),
      () => setNote('Could not turn that off. Try again.'),
    ).finally(() => setBusy(false))
  }
  const list = (kind: OrgKind, rows: OrgRow[], empty: string) => rows.length === 0
    ? <p className="text-text-muted" data-testid={`company-empty-${kind}`}>{empty}</p>
    : <ul className="divide-y divide-border">{rows.map((r) => <Row key={r.name} kind={kind} row={r} busy={busy} onOpen={() => setOpen({ kind, name: r.name })} onOff={() => turnOff(kind, r.name)} />)}</ul>

  return <div className="space-y-4" data-testid="company-tab">
    <div className="flex flex-wrap items-center gap-3">
      <p className="min-w-0 flex-1 text-sm text-text-muted">Skills and tools your company hub offers. Everything is off until you turn it on.</p>
      <Action id="company-sync" disabled={busy} onClick={sync}>Get the latest from your hub</Action>
    </div>
    {note && <p role="status" className="text-sm" data-testid="company-note">{note}</p>}
    <Panel title="Company skills">{list('skill', o.skills, 'Your hub offers no skills.')}</Panel>
    <Panel title="Company tools (MCP servers)">{list('mcp', o.mcp, 'Your hub offers no tools.')}</Panel>
    {open && <ItemSheet ws={ws} kind={open.kind} name={open.name} onClose={() => setOpen(undefined)} />}
  </div>
}
