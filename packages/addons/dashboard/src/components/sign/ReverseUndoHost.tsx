import { useEffect, useState } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { RunnerSwitch } from '@/components/RunnerSwitch'
import { SignShell } from './SignShell'
import { SignPopover } from './SignPopover'

type Reverse = { ws: string; action: Extract<HandoffAction, { action: 'addon_grant' }> } | { ws: string; runner: true }
const listeners = new Set<(request: Reverse) => void>()
export function requestReverse(request: Reverse) {
  for (const listener of listeners) listener(request)
}
/** Undo always opens a fresh authority review; it never reuses a digest or signed event. */
export function ReverseUndoHost() {
  const [request, setRequest] = useState<Reverse | null>(null)
  useEffect(() => {
    listeners.add(setRequest)
    return () => { listeners.delete(setRequest) }
  }, [])
  if (!request) return null
  if ('runner' in request) return <SignShell anchor={null} open onDismiss={() => setRequest(null)} title="Turn on dashboard execution" showClose onOpenAutoFocus={() => {}}><RunnerSwitch ws={request.ws} on initiallyOpen onClosed={() => setRequest(null)} /></SignShell>
  return <SignPopover ws={request.ws} title={`Turn on ${request.action.name}`} action={request.action} onClose={() => setRequest(null)} />
}
