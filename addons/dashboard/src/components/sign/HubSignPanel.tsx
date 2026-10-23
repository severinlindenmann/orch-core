// The hub sign panel (V3 C11, D78/D81): in a workspace joined to a hub in full mode, an approval is signed by the
// person's channel (phone or hub page), never here. The panel has NO Sign button and no local-key path. It shows what
// the HOST says to check: the summary, the 12 characters (compare them with the phone), and one link the host built from
// its pinned hub address. Agent-written text (the title) is plain text below the fixed copy, never a link.
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Loader2, Smartphone } from 'lucide-react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { groupHash, safeHubUrl, type HubSignInfo, type HubSignRequest } from '@/api/hubsign'
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
  return `Decide the ${i.gate ?? ''} step${i.gate_gen != null ? ` (version ${i.gate_gen})` : ''}`
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
        {what(info.summary)} on <Raw>{info.summary.ticket}</Raw>. Your phone also lists it under “Needs you”.
      </p>
      {short ? (
        <div className="rounded-md border border-border bg-bg p-3">
          <p className="text-[12px] text-text-muted">Check that these 12 characters match on your phone before you approve.</p>
          <p data-testid="hub-short-hash" className="mt-1 font-mono text-[18px] tracking-wider text-text">{short}</p>
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
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-text hover:bg-surface-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand"
        >
          Open in the hub <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : (
        <p className="text-text-muted">The host sent no link; approve on your phone.</p>
      )}
      <p className="flex items-center gap-2 text-text-muted" role="status">
        <Loader2 className="size-3.5 animate-spin" aria-hidden /> Waiting for your approval. This page updates when it arrives.
      </p>
      <div className="border-t border-border pt-2">
        <p className="text-[11px] text-text-faint">Ticket title, written by its people and agents:</p>
        <p data-testid="hub-title" className="break-words text-text-muted">
          <Inline>{info.summary.title}</Inline>
        </p>
      </div>
    </div>
  )
}
