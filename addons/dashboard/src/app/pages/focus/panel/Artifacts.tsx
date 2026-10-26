import { useState } from 'react'
import { addonActive } from '@/api/addons'
import { Viewer } from '../../ticket/Artifacts'
import { Empty, usePanelTicket } from './data'

export function Artifacts({ ticket }: { ticket?: string }) {
  const { data, workspace, isError } = usePanelTicket(ticket)
  const [selected, select] = useState<string | null>(null)
  if (!ticket) return <Empty>No ticket yet. Artifacts will appear here.</Empty>
  if (isError) return <Empty>Could not load artifacts.</Empty>
  if (!data) return <Empty>Loading artifacts…</Empty>
  if (!data.artifacts.length) return <Empty>No artifacts yet.</Empty>
  return <ul data-testid="focus-panel-artifacts" className="space-y-2" aria-label="Ticket artifacts">{data.artifacts.map(a => {
    const key = `${ticket}:${a.name}:${a.sha256}`
    const open = selected === key
    return <li key={key} className="min-w-0 border-b border-border pb-2">
      <button type="button" aria-expanded={open} onClick={() => select(open ? null : key)} className="flex w-full items-center justify-between gap-2 rounded p-2 text-left text-sm hover:bg-surface-2"><span className="break-all">{a.name}</span><span className="text-xs text-text-muted">{a.kind}</span></button>
      {open && <section aria-label={`Preview of ${a.name}`} className="min-w-0 overflow-auto py-3"><Viewer a={a} ticket={ticket} agentHtml={addonActive(workspace, 'widgets')} /></section>}
    </li>
  })}</ul>
}
