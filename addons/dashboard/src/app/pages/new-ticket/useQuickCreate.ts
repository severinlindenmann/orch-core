import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { toastApiError } from '@/app/toast'
import { quickRequest } from './quickRules'

/** "Created DEMO-0051" with the title and an Open button; the person stays where they are. */
export function useCreatedToast() {
  const navigate = useNavigate()
  return useCallback(
    (ticket: TicketDocument) =>
      toast.success(`Created ${ticket.key} · ${ticket.type}`, {
        description: ticket.title,
        action: { label: 'Open', onClick: () => void navigate({ to: '/ticket/$key', params: { key: ticket.key } }) },
      }),
    [navigate],
  )
}

/**
 * Quick ticket: the text becomes a backlog ticket through the same host route as the form (`createFromRequest`;
 * a viewer gets the host's 403 as an error toast). Returns the ticket, or null when it was refused.
 */
export function useQuickCreate(workspaceId: string | undefined) {
  const qc = useQueryClient()
  const created = useCreatedToast()
  const busy = useRef(false)
  const [pending, setPending] = useState(false)
  const create = useCallback(
    async (text: string): Promise<TicketDocument | null> => {
      if (!workspaceId || busy.current) return null
      busy.current = true
      setPending(true)
      try {
        const { ticket } = await api.createTicket(workspaceId, quickRequest(text))
        void qc.invalidateQueries()
        created(ticket)
        return ticket
      } catch (e) {
        toastApiError(e, 'Could not create the ticket.')
        return null
      } finally {
        busy.current = false
        setPending(false)
      }
    },
    [workspaceId, qc, created],
  )
  return { create, pending }
}
