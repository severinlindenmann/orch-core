import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { AlertTriangle, X } from 'lucide-react'
import { hubChip, hubServed } from '@/api/hub'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useHub } from '../pages/settings/hub/shared'
import { useOpenHub, useRail } from './CompanyRail'

const DISMISS_KEY = (ws: string) => `orch.hub-link.dismissed.${ws}`

function readDismissed(ws: string | undefined): boolean {
  try {
    return !!ws && localStorage.getItem(DISMISS_KEY(ws)) === '1'
  } catch {
    return false
  }
}

/**
 * The hub is optional, so its top-bar entry is quiet: without a hub a small "optional" link the person can hide (kept
 * per workspace in this browser; Settings → Hub stays), with a healthy hub a plain "Hub" link, and a coloured pill only
 * when something is wrong.
 */
export function HubChip({ ws }: { ws?: string }) {
  const status = useHub(ws)
  const rail = useRail()
  const openHub = useOpenHub(ws)
  const [hidden, setHidden] = useState<{ ws?: string; value: boolean }>({ ws, value: readDismissed(ws) })
  const chip = hubChip(status.data)
  if (!hubServed(ws) || !chip) return null
  const standalone = chip.label === 'Local only'
  const dismissed = hidden.ws === ws ? hidden.value : readDismissed(ws)
  if (standalone && dismissed) return null
  const label = standalone ? 'Connect to hub' : chip.label
  const wrong = chip.tone === 'amber'
  const short = standalone ? 'Hub' : wrong ? chip.short : 'Hub'
  const Icon = chip.icon === 'alert' ? AlertTriangle : null
  const keepsWorking = 'If the hub is offline, your work continues here.'
  const tooltip = status.data?.not_told ? 'You left the hub, but the hub does not know yet. It may still show this workspace as joined. Your work continues here.' :
    status.data?.reachable === false ? 'Work continues here if the hub is offline: keep working and signing. Sharing and phone approvals resume when the hub is back.' :
    wrong ? `The hub connection needs attention. Open Hub settings for details. ${keepsWorking}` :
    status.data?.mode === 'full' ? `Connected to the hub. It can read every ticket, file and note, and hub admins can approve from the hub (see Settings → Hub). You can approve here or on your phone. ${keepsWorking}` :
    status.data?.mode === 'selective' ? `Connected to the hub. It only gets what you pick, plus needs-you notes and questions (see Settings → Hub). You keep signing locally. ${keepsWorking}` :
    'Optional. This workspace works fully on its own: tickets, agents and signing in your terminal. Connect a hub for your phone, publishing and handoffs. Work continues if the hub is offline.'
  const canOpen = (status.data?.mode === 'selective' || status.data?.mode === 'full') && !!rail.data?.hub
  const dismiss = () => {
    try {
      if (ws) localStorage.setItem(DISMISS_KEY(ws), '1')
    } catch {
      /* storage unavailable: hidden for this page only */
    }
    setHidden({ ws, value: true })
  }
  const text = standalone ? <><span className="hub-chip-long">Connect to hub · optional</span><span className="hub-chip-short hidden">Hub</span></> : <><span className="hub-chip-long">{wrong ? chip.label : 'Hub'}</span><span className="hub-chip-short hidden">{short}</span></>
  return <><Tooltip><TooltipTrigger asChild><Link to="/settings/$tab" params={{ tab: 'hub' }} hash={standalone ? 'connect' : undefined} data-testid="hub-chip" aria-label={label}>
    {wrong ? <Badge variant="outline" className="border-warning/40 bg-warning-soft text-warning">{Icon && <Icon aria-hidden />}{text}</Badge>
      : <span className="rounded px-1 text-xs text-text-faint underline-offset-2 hover:text-text-muted hover:underline">{text}</span>}
  </Link></TooltipTrigger><TooltipContent className="max-w-xs">{tooltip}</TooltipContent></Tooltip>
    {standalone && <button type="button" data-testid="hub-chip-dismiss" aria-label="Hide the optional hub link" onClick={dismiss} className="-ml-2 rounded p-0.5 text-text-faint outline-none hover:text-text-muted focus-visible:ring-2 focus-visible:ring-brand"><X className="size-3" aria-hidden /></button>}
    {canOpen && <button type="button" data-testid="hub-open-chip" disabled={openHub.busy} onClick={() => openHub('/')} className="hidden rounded px-1 text-xs text-text-faint outline-none hover:text-text-muted hover:underline focus-visible:ring-2 focus-visible:ring-brand disabled:opacity-60 md:inline-flex">{openHub.busy ? 'Opening hub…' : 'Open hub'}</button>}</>
}
