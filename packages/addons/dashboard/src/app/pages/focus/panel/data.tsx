import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { queries } from '@/api/queries'
import { useWorkspace } from '@/app/workspace'
import type { GateName, TicketDocument } from '@/api/types'

export function usePanelTicket(ticket?: string) {
  const workspace = useWorkspace().workspace
  const query = useQuery({ ...queries.ticket(workspace?.id ?? '', ticket ?? ''), enabled: !!workspace && !!ticket })
  return { ...query, workspace }
}
export function useChanges(ticket?: string) {
  const detail = usePanelTicket(ticket)
  return useQuery({ queryKey: ['changes', detail.workspace?.id, ticket, detail.data?.branch.head], queryFn: () => api.getChanges(ticket!, detail.workspace!.id), enabled: !!detail.workspace && !!detail.data })
}
export function decisionGates(ticket?: TicketDocument): GateName[] {
  if (!ticket) return []
  return (['requirements', 'plan', 'verify', 'code'] as const).filter(g => g !== 'code' || ticket.gates.code.required || ticket.gates.code.approvals.length || ticket.gates.code.voided?.length)
}
export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-6 text-sm text-text-muted">{children}</p>
}
