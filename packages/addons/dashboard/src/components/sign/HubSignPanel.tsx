// The hub sign panel (V3 C11, D78/D81): in a workspace joined to a hub in full mode, an approval is signed by the
// person's channel (phone or hub page), never here. The panel has NO Sign button and no local-key path. It shows what
// the HOST says to check: the summary, the 12 characters (compare them with the phone), and one link the host built from
// its pinned hub address. Agent-written text (the title) is plain text below the fixed copy, never a link.
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Loader2, Smartphone } from 'lucide-react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { useEffect, useRef } from 'react'
import { groupHash, safeHubUrl, type HubSignInfo, type HubSignRequest } from '@/api/hubsign'
import type { HandoffAction } from '@/api/handoff'
import { useHandoff } from './useHandoff'
import { Inline, Raw } from './visible'

const WAYS = 'Approve on your phone or in the hub'

/** Fixed copy for an action the hub cannot build yet (grant, settings, devices): no local fallback exists (D78). */
export function NotFromHub() {
  return (
    <div data-testid="hub-unavailable" className="space-y-2 text-[13px]">
      <p className="font-medium text-text">Not available from the hub yet</p>
      <p className="text-text-muted">
        This workspace takes approvals through the hub only. To stop an agent now, run <Raw>orch grant forget</Raw> in your terminal, or leave the hub with <Raw>orch hub leave</Raw>.
      </p>
    </div>
  )
}

function what(i: HubSignInfo['summary']): string {
  if (i.kind === 'question') return `Answer question ${i.question ?? ''}`.trim()
  if (i.kind === 'verdict') return 'Give the verdict'
  if (i.kind === 'status') return 'Move the ticket'
  return `Approve ${i.gate_gen != null ? `version ${i.gate_gen} of the` : 'the'} ${i.gate ?? ''} step`
}

export function HubSignPanel({ ws, request }: { ws: string; request: HubSignRequest }) {
  const q = useQuery({
    queryKey: ['hub-sign', ws, request],
    queryFn: () => api.hubSign(ws, request),
    retry: false,
    refetchInterval: 5000, // the hub's event arrives through the live feed; this keeps the hash and the link current
  })
  if (q.isPending)
    return (
      <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Asking the host…
      </p>
    )
  if (q.isError && !q.data) {
    const code = q.error instanceof ApiError ? q.error.code : ''
    return (
      <div role="alert" data-testid="hub-paused" className="space-y-2 text-[13px]">
        <p className="font-medium text-text">Approvals are paused: the hub is not reachable</p>
        <p className="text-text-muted">
          Nothing can be signed here in the meantime{code ? ` (${code})` : ''}. To leave the hub, run <Raw>orch hub leave</Raw> in your terminal.
        </p>
      </div>
    )
  }
  const info = q.data!
  const short = info.short_hash === null ? null : groupHash(info.short_hash)
  const url = safeHubUrl(info.url)
  return (
    <div data-testid="hub-sign" className="space-y-3 text-[13px]">
      <p className="flex items-center gap-2 font-medium text-text">
        <Smartphone className="size-4 text-brand" aria-hidden /> {WAYS}
      </p>
      <p className="text-text-muted">
        {what(info.summary)} on <Raw>{info.summary.ticket}</Raw>. On your phone, open “Needs you” and pick this request.
      </p>
      {short ? (
        <div className="rounded-md border border-border bg-bg p-3">
          <p className="text-[12px] text-text-muted">Check that these 12 characters match on your phone before you approve.</p>
          <p className="text-[12px] text-text-muted">If they differ, do not approve.</p>
          <p data-testid="hub-short-hash" className="mt-1 font-mono text-[22px] tracking-widest text-text">{short}</p>
        </div>
      ) : (
        <p className="text-text-muted" data-testid="hub-no-hash">
          {info.summary.kind === 'status' ? 'A status move has no code to compare: check the ticket and the move on your phone.' : 'No code to compare is available for this item.'}
        </p>
      )}
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="hub-link"
          className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-border px-4 py-2 text-text hover:bg-surface-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand"
        >
          Open in the hub <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : (
        <p className="text-text-muted">The host sent no link; approve on your phone.</p>
      )}
      <p className="flex items-center gap-2 text-text-muted" role="status">
        <Loader2 className="size-3.5 animate-spin" aria-hidden /> Waiting for you to approve on your phone or in the hub. This page updates by itself.
      </p>
      <div data-testid="hub-untrusted" className="border-t border-dashed border-border pt-2 opacity-80">
        <p className="text-[11px] italic text-text-faint">Ticket title, written by its people and agents (not checked):</p>
        <p data-testid="hub-title" className="break-words text-[12px] text-text-muted">
          “<Inline>{info.summary.title}</Inline>”
        </p>
      </div>
    </div>
  )
}

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
      <p className="flex items-center gap-2 text-text-muted" role="status"><Loader2 className="size-3.5 animate-spin" aria-hidden /> Waiting for your approval in the hub. This page updates by itself.</p>
    </div>
  )
}
