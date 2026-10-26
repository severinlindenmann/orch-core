import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useCallback, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { toastApiError } from '@/app/toast'
import { quickRequest } from './quickRules'

/**
 * "Created DEMO-0051 · bug" with the title, Open and Undo; the person stays where they are. One toast per ticket
 * (`id` = the key). Undo removes the ticket through the host, which refuses once anything else happened to it.
 */
export function useCreatedToast() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  return useCallback(
    (ticket: TicketDocument, workspaceId: string) =>
      toast.success(`Created ${ticket.key} · ${ticket.type}`, {
        id: ticket.key,
        description: ticket.title,
        action: { label: 'Open', onClick: () => void navigate({ to: '/ticket/$key', params: { key: ticket.key } }) },
        cancel: {
          label: 'Undo',
          onClick: async () => {
            try {
              await api.undoCreateTicket(workspaceId, ticket.key)
              await qc.invalidateQueries()
              toast.success(`Removed ${ticket.key}`, { id: ticket.key, description: ticket.title })
            } catch (e) {
              toastApiError(e, `Could not undo ${ticket.key}.`)
            }
          },
        },
      }),
    [navigate, qc],
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
        created(ticket, workspaceId)
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
