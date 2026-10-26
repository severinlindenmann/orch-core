import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useBlocker, useNavigate } from '@tanstack/react-router'
import { queries } from '@/api/queries'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { NewTicketForm } from '../new-ticket'

export function CreateFocusTicket({ onClose }: { onClose: () => void }) {
  const { workspace } = useWorkspace()
  const { data: me } = useMe()
  const navigate = useNavigate()
  // The shared form's signed flow navigates to the created ticket itself. Keep that
  // successful destination in Focus without changing the form or its signing flow.
  const signed = useRef(false)
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
    void navigate({ to: '/focus/$ticket', params: { ticket } })
  }, [blocker, navigate])
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader><DialogTitle>New ticket</DialogTitle><DialogDescription>Create a ticket for this focus session.</DialogDescription></DialogHeader>
        {workspace && me ? <NewTicketForm me={me} workspace={workspace} variant="overlay" onCancel={onClose} onClosed={() => { signed.current = true }} onCreated={(ticket) => {
          onClose()
          void navigate({ to: '/focus/$ticket', params: { ticket: ticket.key } })
        }} /> : <p>Loading ticket form...</p>}
      </DialogContent>
    </Dialog>
  )
}

export function StartDialog() {
  const { workspace } = useWorkspace()
  const navigate = useNavigate()
  const [choice, setChoice] = useState<'new' | 'existing' | null>(null)
  const tickets = useQuery({ ...queries.tickets(workspace?.id ?? '', {}), enabled: !!workspace && choice === 'existing' })
  const open = tickets.data?.filter((ticket) => ticket.status !== 'done' && ticket.status !== 'closed')
  return (
    <section data-testid="focus-start" aria-labelledby="focus-start-title" className="mx-auto w-full max-w-xl space-y-4 p-6">
      <h1 id="focus-start-title" className="text-xl font-semibold">Start focus session</h1>
      <p className="text-sm text-text-muted">Choose a ticket or start with a blank workspace.</p>
      <div className="grid gap-3">
        <Button variant="outline" onClick={() => setChoice('new')}>New ticket with me</Button>
        <Button variant="outline" onClick={() => setChoice('existing')} aria-expanded={choice === 'existing'}>Existing ticket</Button>
        <Button variant="outline" onClick={() => void navigate({ to: '/focus', search: { scratch: 1 } })}>No ticket, just work</Button>
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
      {choice === 'new' && <CreateFocusTicket onClose={() => setChoice(null)} />}
    </section>
  )
}
