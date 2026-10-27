// The hub sign panel (optional extra path, A1/D83): an approval may be signed by the person's channel (phone or hub
// page) instead of here, when the person asks for it. The panel has NO Sign button and no local-key path. It shows what
// the HOST says to check: the summary, the 12 characters (compare them with the phone), and one link the host built from
// its pinned hub address. Agent-written text (the title) is plain text below the fixed copy, never a link.
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Loader2 } from 'lucide-react'
import { api } from '@/api/client'
import { useEffect, useRef } from 'react'
import { HUB_SETTING_WORDS, groupHash, hubSettingsSplit, safeHubUrl } from '@/api/hubsign'
import { queries } from '@/api/queries'
import type { HandoffAction } from '@/api/handoff'
import { useHandoff } from './useHandoff'
import { Inline } from './visible'


const CAPS: Record<string, string> = { serve_http: 'run a small web page', spawn_agent: 'start agents', pty: 'open terminals', network: 'use the network', git_push: 'push code', choose_model: 'choose which AI model runs', credential: 'keep a secret key' }

/** FB15: turning an add-on on in a full-mode workspace. The host prepares the grant as a hand-off (nothing is signed here)
 *  and names it; the person approves it in the hub with plain clicks, and this panel follows the hand-off to its end. */
export function HubAddonPanel({ ws, action, onDone }: { ws: string; action: Extract<HandoffAction, { action: 'addon_grant' | 'market_grant' }>; onDone: () => void }) {
  const run = useHandoff(ws, (phase) => { if (phase === 'done') onDone() })
  const { prepare } = run
  const started = useRef(false) // prepare once: a StrictMode re-run or a new `action` identity must not make a second request
  useEffect(() => { if (started.current) return; started.current = true; void prepare(action) }, [prepare, action])
  const rid = run.request?.id
  const q = useQuery({ queryKey: ['hub-addon', ws, rid], enabled: !!rid && run.phase === 'waiting', queryFn: () => api.hubSign(ws, { kind: 'addon', request: rid! }), retry: false })
  if (run.phase === 'done') return <p role="status" className="text-[13px] text-text">Approved in the hub. The add-on is set up.</p>
  if (run.error || ['expired', 'cancelled', 'failed', 'error'].includes(run.phase))
    return <p role="alert" data-testid="hub-addon-stopped" className="text-[13px] text-text-muted">{run.error?.message ?? 'The request ended before it was approved. Close this and try again.'}</p>
  if (!rid || q.isPending)
    return <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted"><Loader2 className="size-4 animate-spin" aria-hidden /> Preparing the request…</p>
  if (q.isError || !q.data) return <p role="alert" className="text-[13px] text-text-muted">The hub link could not be made. Nothing was changed.</p>
  const info = q.data
  const url = safeHubUrl(info.url)
  const otherUrl = safeHubUrl(info.other_url)
  const caps = info.summary.capabilities ?? []
  return (
    <div data-testid="hub-addon" className="space-y-3 text-[13px]">
      <p className="font-medium text-text">Approve in the hub</p>
      <p className="text-text-muted">This workspace takes approvals through the hub. Open the hub, check the details there and approve with one click.</p>
      <div className="rounded-md border border-border bg-bg p-3">
        <p className="text-text"><Inline>{info.summary.name ?? ''}</Inline> <span className="text-text-muted"><Inline>{info.summary.version ?? ''}</Inline></span></p>
        <p className="text-[12px] text-text-muted">{caps.length ? `It may: ${caps.map((c) => CAPS[c] ?? c).join(', ')}.` : 'It asks for no extra access.'}</p>
        {info.short_hash && <p className="text-[12px] text-text-muted">Package code <span data-testid="hub-short-hash" className="font-mono">{groupHash(info.short_hash)}</span></p>}
      </div>
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer" data-testid="hub-link" className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-border px-4 py-2 text-text hover:bg-surface-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand">
          Open the hub to approve <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : <p className="text-text-muted">The host sent no link.</p>}
      {url && otherUrl && (
        <p data-testid="hub-link-other" className="break-all text-[12px] text-text-muted">
          That link is for this computer. To approve from another device, open <span className="font-mono">{otherUrl}</span> there.
        </p>
      )}
      <p className="flex items-center gap-2 text-text-muted" role="status"><Loader2 className="size-3.5 animate-spin" aria-hidden /> Waiting for your approval in the hub. This page updates by itself.</p>
    </div>
  )
}

const OFF_WORDS: Record<string, string> = { repos: 'the code folders', models: 'the AI models', icon: 'the workspace icon' }

function hours(v: number, unit: string): string {
  if (unit === 'hours') return v % 24 === 0 ? `${v / 24} day${v === 24 ? '' : 's'}` : `${v} hours`
  return `${v} minutes`
}

/** A settings change in a full-mode workspace. The hub's channel may set three timings; they go to the hub as one link and
 *  this panel follows the host's settings until they read as asked. Anything else is said plainly: it is not a hub setting. */
export function HubSettingsPanel({ ws, action, onDone }: { ws: string; action: Extract<HandoffAction, { action: 'settings_set' }>; onDone: () => void }) {
  const split = hubSettingsSplit(action)!
  const hasSet = Object.keys(split.set).length > 0
  const q = useQuery({ queryKey: ['hub-settings-link', ws, split.set], enabled: hasSet && split.off.length === 0, queryFn: () => api.hubSign(ws, { kind: 'settings', set: split.set }), retry: false })
  const now = useQuery({ ...queries.hostSettings(ws), refetchInterval: 3000, enabled: hasSet && split.off.length === 0 })
  const applied = hasSet && !!now.data && Object.entries(split.set).every(([k, v]) => (now.data as unknown as Record<string, unknown>)[k] === v)
  const fired = useRef(false)
  useEffect(() => { if (applied && !fired.current) { fired.current = true; onDone() } }, [applied, onDone])
  if (split.off.length > 0 || !hasSet) {
    const names = split.off.map((k) => OFF_WORDS[k] ?? k).join(', ')
    return (
      <div data-testid="hub-settings-off" className="space-y-2 text-[13px]">
        <p className="font-medium text-text">This cannot be changed from the hub</p>
        <p className="text-text-muted">
          {names ? `The hub can only change how long agents keep access and how long tickets and tasks are held, not ${names}. ` : ''}
          Those settings are signed with your own key on the computer that holds the workspace, which a hub-managed workspace does not have. To change them, leave the hub first in your terminal.
        </p>
      </div>
    )
  }
  if (applied) return <p role="status" className="text-[13px] text-text">Approved in the hub. The setting is changed.</p>
  if (q.isPending) return <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted"><Loader2 className="size-4 animate-spin" aria-hidden /> Preparing the request…</p>
  if (q.isError || !q.data) return <p role="alert" className="text-[13px] text-text-muted">The hub link could not be made. Nothing was changed.</p>
  const url = safeHubUrl(q.data.url)
  return (
    <div data-testid="hub-settings" className="space-y-3 text-[13px]">
      <p className="font-medium text-text">Approve in the hub</p>
      <p className="text-text-muted">This workspace takes approvals through the hub. Open the hub, check the change there and approve with one click.</p>
      <ul className="rounded-md border border-border bg-bg p-3">
        {Object.entries(split.set).map(([k, v]) => <li key={k} className="text-text">{HUB_SETTING_WORDS[k].label}: {hours(v, HUB_SETTING_WORDS[k].unit)}</li>)}
      </ul>
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer" data-testid="hub-link" className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-border px-4 py-2 text-text hover:bg-surface-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand">
          Open the hub to approve <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : <p className="text-text-muted">The host sent no link.</p>}
      <p className="flex items-center gap-2 text-text-muted" role="status"><Loader2 className="size-3.5 animate-spin" aria-hidden /> Waiting for your approval in the hub. This page updates by itself.</p>
    </div>
  )
}
