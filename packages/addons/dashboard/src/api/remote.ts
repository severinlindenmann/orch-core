// Remote access, the loopback page (R2 T4, orch-core #1590). The server relays the host's reads in a closed shape
// (server remote_routes.py `view`). Turning ON and confirming a device are signed by click (`remote_set`,
// `remote_device_confirm`, with the backup key); turning off, a device code and removing a device are plain owner clicks.
import { queryOptions } from '@tanstack/react-query'
import { api } from '@/api/client'
import { hasFeature, onThisComputer } from './features'

export type RemoteLevel = 'view' | 'work' | 'terminals'
export type Premises = 'yes' | 'no'

export interface RemoteDevice {
  device_id: string
  name: string
  level_cap: RemoteLevel
  added_ms: number | null
  expires_ms: number | null
  /** The six words of the device's fingerprint. */
  words: string
}
export interface RemotePending {
  pending: string
  name: string
  words: string
  expires_ms: number | null
}
export interface RemoteView {
  on: boolean
  level: RemoteLevel | null
  until_ms: number | null
  premises: Premises | null
  off_reason: string | null
  /** `false`: agents on this computer can act as the person (#1198 H1). `null`: the host does not say. */
  isolated: boolean | null
  hub_mode: 'none' | 'selective' | 'full'
  devices: RemoteDevice[]
  pending: RemotePending[]
  sessions: { device: string; device_id: string; stream_id: string; level: RemoteLevel | null; since_ms: number | null }[]
}
export interface RemoteCode { code: string; expires_ms: number | null }

/** Is the section offered? Only on the computer itself: a remote session never reaches these routes. */
export const remoteServed = (ws: string | undefined): boolean => hasFeature('remote', ws) && onThisComputer()

export const remoteQueries = {
  status: (ws: string, watch = false) => queryOptions({
    queryKey: ['remote', ws, 'status'],
    queryFn: () => api.getRemote(ws),
    staleTime: 5_000,
    refetchInterval: watch ? 3_000 : false,
    retry: false,
  }),
}

export const LEVEL_NAME: Record<RemoteLevel, string> = { view: 'View', work: 'Work', terminals: 'Terminals' }
export const LEVEL_HELP: Record<RemoteLevel, string> = {
  view: 'Look at tickets, files and notes.',
  work: 'Also answer questions, approve and start agents.',
  terminals: 'A command line on this computer. The strongest level.',
}
export const LEVEL_RANK: Record<RemoteLevel, number> = { view: 0, work: 1, terminals: 2 }

/** "until 3 Nov, 14:20", in local time. */
export const untilText = (ms: number | null): string =>
  ms === null ? 'until you turn it off'
    : 'until ' + new Date(ms).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
