// Hub and dashboard as one app (H1, 27 Oct). The hub is the only authority for a switch ticket; this file only asks the
// dashboard server for one (`POST /api/w/<ws>/hub/switch`) and for the company rail (`GET /api/w/<ws>/hub/rail`, fetched
// by the server through the host's hub link). A ticket travels in the URL fragment only.
import { switchTransport } from '@/api/client'

export interface RailRow {
  workspace_id: string
  name: string | null
  role: 'read' | 'edit' | 'admin' | 'owner' | null
  online: boolean
  badge: number
  origin: string | null
  current: boolean
}
export interface Rail { hub: { name: string; origin: string | null } | null; workspaces: RailRow[] }

const transport = () => switchTransport

export async function fetchRail(ws: string): Promise<Rail> {
  const res = await transport().request('GET', `/api/w/${ws}/hub/rail`)
  const json = res.json as Rail | null
  if (res.status !== 200 || !json || !Array.isArray(json.workspaces)) return { hub: null, workspaces: [] }
  return json
}

/** The address that opens the hub console (`open`) or signs out of it (`signout`), with the ticket in its fragment. */
export async function hubUrl(ws: string, kind: 'open' | 'signout', returnTo = '/'): Promise<string> {
  const res = await transport().request('POST', `/api/w/${ws}/hub/switch`, { return_to: returnTo, kind })
  const url = (res.json as { url?: unknown } | null)?.url
  if (res.status !== 200 || typeof url !== 'string') throw new Error('The hub could not be reached. Try again in a moment.')
  const target = new URL(url)
  if (target.protocol !== 'https:' && target.protocol !== 'http:') throw new Error('The hub address is not usable.')
  return target.href
}
