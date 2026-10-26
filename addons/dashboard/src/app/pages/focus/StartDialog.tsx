import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { queries } from '@/api/queries'
import { isRealHost } from '@/api/features'
import { newIdem, runsApi, type TermArgs } from '@/api/runs'
import { ApiError } from '@/api/types'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { NewTicketForm, type Draft } from '../new-ticket'

export function CreateFocusTicket({ onClose, initial, onTicket }: { onClose: () => void; initial?: Partial<Draft>; onTicket?: (key: string) => Promise<void> | void }) {
  const { workspace } = useWorkspace()
  const { data: me } = useMe()
  const navigate = useNavigate()
  // The shared form's signed flow navigates to the created ticket itself. Keep that
  // successful destination in Focus without changing the form or its signing flow.
  const signed = useRef(false)
  // Promote (P2-6): the scratch session is linked to the new ticket before Focus opens it.
  async function arrive(ticket: string) {
    try { await onTicket?.(ticket) } catch { /* The ticket exists; the link can be retried from the ticket's session. */ }
    void navigate({ to: '/focus/$ticket', params: { ticket } })
  }
  const blocker = useBlocker({
    shouldBlockFn: ({ next }) => signed.current && next.routeId === '/ticket/$key',
    withResolver: true,
    enableBeforeUnload: false,
  })
  useEffect(() => {
    if (blocker.status !== 'blocked' || blocker.next.routeId !== '/ticket/$key') return
    const ticket = blocker.next.params.key
    signed.current = false
    blocker.reset()
    void arrive(ticket)
  }, [blocker, navigate]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader><DialogTitle>New ticket</DialogTitle><DialogDescription>Create a ticket for this focus session.</DialogDescription></DialogHeader>
        {workspace && me ? <NewTicketForm me={me} workspace={workspace} variant="overlay" initial={initial} onCancel={onClose} onClosed={() => { signed.current = true }} onCreated={(ticket) => {
          onClose()
          void arrive(ticket.key)
        }} /> : <p>Loading ticket form...</p>}
      </DialogContent>
    </Dialog>
  )
}

/** "No ticket, just work": a person-run Claude terminal in the workspace folder; the preview is what the host will start. */
function ScratchStart({ ws, onCancel }: { ws: string; onCancel: () => void }) {
  const navigate = useNavigate()
  const args: TermArgs = { harness: 'claude-code', cwd: { kind: 'workspace' } }
  const idem = useRef(newIdem())
  const preview = useQuery({ queryKey: ['focus', 'scratch-preview', ws], queryFn: () => runsApi.preview(ws, args), retry: false, staleTime: 0, gcTime: 0 })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function open() {
    if (!preview.data || preview.data.blocked || busy) return
    setBusy(true)
    setError('')
    try {
      const { run } = await runsApi.open(ws, args, preview.data.spec, idem.current)
      void navigate({ to: '/focus', search: { scratch: run.run } })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'The host did not answer.')
      setBusy(false)
    }
  }
  const blocked = preview.data?.blocked
  return <div data-testid="focus-scratch-preview" className="space-y-2 rounded-md border border-border bg-surface p-3 text-sm">
    {preview.isPending ? <p role="status">Checking what will start...</p> : preview.isError ? <p role="alert">Could not check the start. <Button variant="ghost" onClick={() => void preview.refetch()}>Try again</Button></p> : blocked ? <p role="alert">{blocked.message ?? 'This session cannot start here.'}</p> : <p>Starts Claude as you in the workspace folder{preview.data?.display ? <>: <code className="break-all font-mono text-xs">{preview.data.display}</code></> : ''}. Nothing is tied to a ticket until you create one.</p>}
    {error && <p role="alert">{error}</p>}
    <div className="flex gap-2"><Button size="sm" disabled={busy || !preview.data || !!blocked} onClick={() => void open()}>{busy ? 'Starting...' : 'Start'}</Button><Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button></div>
  </div>
}

export function StartDialog() {
  const { workspace } = useWorkspace()
  const navigate = useNavigate()
  const [choice, setChoice] = useState<'new' | 'existing' | 'scratch' | null>(null)
  const tickets = useQuery({ ...queries.tickets(workspace?.id ?? '', {}), enabled: !!workspace && choice === 'existing' })
  const open = tickets.data?.filter((ticket) => ticket.status !== 'done' && ticket.status !== 'closed')
  return (
    <section data-testid="focus-start" aria-labelledby="focus-start-title" className="mx-auto w-full max-w-xl space-y-4 p-6">
      <h1 id="focus-start-title" className="text-xl font-semibold">Start focus session</h1>
      <p className="text-sm text-text-muted">Choose a ticket or start with a blank workspace.</p>
      <div className="grid gap-3">
        <Button variant="outline" onClick={() => setChoice('new')}>New ticket with me</Button>
        <Button variant="outline" onClick={() => setChoice('existing')} aria-expanded={choice === 'existing'}>Existing ticket</Button>
        <Button variant="outline" onClick={() => { if (isRealHost()) setChoice('scratch'); else void navigate({ to: '/focus', search: { scratch: 1 } }) }}>No ticket, just work</Button>
      </div>
      {choice === 'existing' && <div className="space-y-2">
        <label htmlFor="focus-ticket" className="text-sm">Open tickets</label>
        {tickets.isPending ? <p role="status">Loading tickets...</p> : tickets.isError ? <p role="alert">Could not load tickets. <Button variant="ghost" onClick={() => void tickets.refetch()}>Try again</Button></p> : open?.length ? (
          <select id="focus-ticket" defaultValue="" className="w-full rounded-md border border-border bg-surface p-2 text-sm text-text" onChange={(event) => {
            if (event.target.value) void navigate({ to: '/focus/$ticket', params: { ticket: event.target.value } })
          }}>
            <option value="" disabled>Choose a ticket</option>
            {open.map((ticket) => <option key={ticket.key} value={ticket.key}>{ticket.key} · {ticket.title}</option>)}
          </select>
        ) : <p className="text-sm text-text-muted">No open tickets.</p>}
      </div>}
      {choice === 'scratch' && workspace && <ScratchStart ws={workspace.id} onCancel={() => setChoice(null)} />}
      {choice === 'new' && <CreateFocusTicket onClose={() => setChoice(null)} />}
    </section>
  )
}
