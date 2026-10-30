// The proxy for hub links (R6 #1594): where this computer's hub connection goes, set by the owner on this computer.
// The password goes to the host and never comes back; see server network_routes.py.
import { queryOptions } from '@tanstack/react-query'
import { api } from '@/api/client'

export interface NetworkSettings { proxy: string | null; user: string | null; has_password: boolean; broken: boolean }
export interface NetworkChange { proxy: string | null; user?: string | null; password?: string | null }

export const networkQueries = {
  get: (ws: string) => queryOptions({ queryKey: ['hub', ws, 'network'], queryFn: () => api.getHubNetwork(ws), retry: false }),
}
