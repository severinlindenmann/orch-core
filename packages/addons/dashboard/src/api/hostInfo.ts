// Core D1 (#582): what the host says about itself when it is a dev sandbox host (`host.list.dev`, `host.list.alarms`).
// Every field is optional and a missing one means the old behaviour: a normal host sends none of it. The server passes
// a checked shape; the page checks again and shows only what it knows (an unknown alarm code is shown as a code, never as text).

export interface HostDev {
  sandbox: string | null
}
export interface HostAlarm {
  /** A closed code of the host; an unknown one is drawn as "unknown alarm" plus the code. */
  code: string
  /** Only when it is 32 lowercase hex (checked here). */
  workspace_id: string | null
}
export interface HostInfo {
  dev: HostDev | null
  alarms: HostAlarm[]
}

const WORKSPACE_ID = /^[0-9a-f]{32}$/
const SANDBOX_ID = /^[0-9a-f]{8}$/
const CODE = /^[a-z][a-z0-9_]{0,63}$/
const MAX_ALARMS = 16

export const NO_HOST_INFO: HostInfo = { dev: null, alarms: [] }

/** The checked host info of a server answer; anything unexpected reads as "nothing to say" (the old behaviour). */
export function hostInfoOf(raw: unknown): HostInfo {
  if (!raw || typeof raw !== 'object') return NO_HOST_INFO
  const r = raw as Record<string, unknown>
  let dev: HostDev | null = null
  if (r.dev === true) dev = { sandbox: null }
  else if (r.dev && typeof r.dev === 'object' && (r.dev as Record<string, unknown>).sim_convenience === true) {
    const s = (r.dev as Record<string, unknown>).sandbox
    dev = { sandbox: typeof s === 'string' && SANDBOX_ID.test(s) ? s : null }
  }
  const alarms: HostAlarm[] = []
  if (Array.isArray(r.alarms)) {
    for (const a of r.alarms.slice(0, MAX_ALARMS)) {
      if (!a || typeof a !== 'object') continue
      const o = a as Record<string, unknown>
      if (typeof o.code !== 'string' || !CODE.test(o.code)) continue
      alarms.push({
        code: o.code,
        workspace_id: typeof o.workspace_id === 'string' && WORKSPACE_ID.test(o.workspace_id) ? o.workspace_id : null,
      })
    }
  }
  return { dev, alarms }
}

export const SANDBOX_REGISTERED = 'sandbox_workspace_registered'
export const DEV_CONVENIENCE_REFUSED = 'dev_convenience_refused'
