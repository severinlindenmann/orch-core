import { Link } from '@tanstack/react-router'
import { AlertTriangle } from 'lucide-react'
import { hubChip, hubServed } from '@/api/hub'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useHub } from '../pages/settings/hub/shared'

export function HubChip({ ws }: { ws?: string }) {
  const status = useHub(ws)
  const chip = hubChip(status.data)
  if (!hubServed(ws) || !chip) return null
  const label = chip.label === 'Local only' ? 'Only on this computer' : chip.label
  const short = chip.label === 'Local only' ? label : chip.short
  const Icon = chip.icon === 'alert' ? AlertTriangle : null
  const tooltip = status.data?.not_told ? 'You left the hub, but the hub does not know yet. It may still show this workspace as joined.' :
    status.data?.reachable === false ? (status.data.mode === 'full' ? 'Approvals are paused while the hub is offline.' : 'Keep working and signing locally. Shared updates wait for the hub.') :
    chip.tone === 'amber' ? 'The hub connection needs attention. Open Hub settings for details.' :
    status.data?.mode === 'full' ? 'Connected to the hub. It can read every ticket, file and note, and hub admins act as admins here (see Settings → Hub). Approvals use your phone or passkey.' :
    status.data?.mode === 'selective' ? 'Connected to the hub. It only gets what you pick, plus needs-you notes and questions (see Settings → Hub). You keep signing locally.' :
    'This workspace works fully on its own: tickets, agents and signing in your terminal. Connect a hub for your phone, publishing and handoffs.'
  const tone = chip.tone === 'amber' ? 'border-warning/40 bg-warning-soft text-warning' : 'border-border bg-surface-2 text-text-muted'
  return <Tooltip><TooltipTrigger asChild><Link to="/settings/$tab" params={{ tab: 'hub' }} data-testid="hub-chip" aria-label={label}>
    <Badge variant="outline" className={tone}>{Icon && <Icon aria-hidden />}{chip.icon === 'dot' && <span aria-hidden className="size-1.5 rounded-full bg-success" />}<span className="hub-chip-long">{label}</span><span className="hub-chip-short hidden">{short}</span></Badge>
  </Link></TooltipTrigger><TooltipContent className="max-w-xs">{tooltip}</TooltipContent></Tooltip>
}
