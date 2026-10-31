import { Link } from '@tanstack/react-router'
import { AlertTriangle } from 'lucide-react'
import { hubChip, hubServed } from '@/api/hub'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useHub } from '../pages/settings/hub/shared'
import { useOpenHub, useRail } from './CompanyRail'

export function HubChip({ ws }: { ws?: string }) {
  const status = useHub(ws)
  const rail = useRail()
  const openHub = useOpenHub(ws)
  const chip = hubChip(status.data)
  if (!hubServed(ws) || !chip) return null
  const standalone = chip.label === 'Local only'
  const label = standalone ? 'Connect to hub' : chip.label
  const short = standalone ? 'Connect' : chip.short
  const Icon = chip.icon === 'alert' ? AlertTriangle : null
  const tooltip = status.data?.not_told ? 'You left the hub, but the hub does not know yet. It may still show this workspace as joined.' :
    status.data?.reachable === false ? 'Keep working and signing here. Sharing and phone approvals resume when the hub is back.' :
    chip.tone === 'amber' ? 'The hub connection needs attention. Open Hub settings for details.' :
    status.data?.mode === 'full' ? 'Connected to the hub. It can read every ticket, file and note, and hub admins can approve from the hub (see Settings → Hub). You can approve here or on your phone.' :
    status.data?.mode === 'selective' ? 'Connected to the hub. It only gets what you pick, plus needs-you notes and questions (see Settings → Hub). You keep signing locally.' :
    'This workspace works fully on its own: tickets, agents and signing in your terminal. Connect a hub for your phone, publishing and handoffs.'
  const tone = chip.tone === 'amber' ? 'border-warning/40 bg-warning-soft text-warning' : 'border-border bg-surface-2 text-text-muted'
  const canOpen = (status.data?.mode === 'selective' || status.data?.mode === 'full') && !!rail.data?.hub
  return <><Tooltip><TooltipTrigger asChild><Link to="/settings/$tab" params={{ tab: 'hub' }} hash={standalone ? 'connect' : undefined} data-testid="hub-chip" aria-label={label}>
    <Badge variant="outline" className={standalone ? 'border-brand bg-transparent text-brand hover:bg-surface-2' : tone}>{Icon && <Icon aria-hidden />}{chip.icon === 'dot' && <span aria-hidden className="size-1.5 rounded-full bg-success" />}<span className="hub-chip-long">{label}</span><span className="hub-chip-short hidden">{short}</span></Badge>
  </Link></TooltipTrigger><TooltipContent className="max-w-xs">{tooltip}</TooltipContent></Tooltip>
    {canOpen && <button type="button" data-testid="hub-open-chip" onClick={() => openHub('/')} className="hidden rounded-full border border-border px-2.5 py-0.5 text-xs text-brand outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-brand md:inline-flex">Open hub</button>}</>
}
