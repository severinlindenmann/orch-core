/** The remote client's home page talks only to its own server (same origin, `/api/remote-client/...`), with the bearer it got
 * for the single-use launch code in the address. It never speaks to the hub or to a workspace. */

export type Row = {
  workspace_id: string
  online: boolean
  added: boolean
  name: string
  name_from_hub: boolean
  hub_name: string
  open: boolean
  ended: string | null
}

export type Prompt = { id: string; workspace: string; lines: string[] }

export type State = {
  signed_in: boolean
  hub: { id: string; address: string } | null
  presence: { available: boolean; method: 'touch_id' | 'key'; tied: boolean | null }
  prompt: Prompt | null
  open: string[]
  ended: Record<string, string>
  computer: string
}

export type EnrollStatus = { status: 'starting' | 'words' | 'added' | 'error' | 'stopped'; words: string[]; message: string; workspace_id: string }

export class ApiError extends Error {
  constructor(message: string, readonly code: string) {
    super(message)
  }
}

export type Api = {
  state(): Promise<State>
  connect(f: { link: string; user: string; password: string; code: string }): Promise<void>
  workspaces(): Promise<Row[]>
  enrollStart(code: string): Promise<{ id: string; workspace_id: string }>
  enrollStatus(id: string): Promise<EnrollStatus>
  enrollStop(id: string): Promise<void>
  open(workspaceId: string): Promise<void>
  end(workspaceId: string): Promise<void>
  answer(id: string, approve: boolean): Promise<void>
}

const STORAGE = 'orch-remote-home'

/** Turn the launch code in the address (`#launch=`) into the page's bearer; the code leaves the address at once. */
export async function launch(fetcher: typeof fetch = fetch): Promise<string | null> {
  const code = new URLSearchParams(location.hash.slice(1)).get('launch')
  if (code) {
    history.replaceState(null, '', location.pathname)
    const res = await fetcher('/auth/session', {
      method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ code }),
    })
    if (!res.ok) return null
    const body = (await res.json()) as { token?: string }
    if (typeof body.token !== 'string') return null
    try { sessionStorage.setItem(STORAGE, body.token) } catch { /* the page then works until it is reloaded */ }
    return body.token
  }
  try { return sessionStorage.getItem(STORAGE) } catch { return null }
}

export function createApi(token: string, fetcher: typeof fetch = fetch): Api {
  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetcher(`/api/remote-client${path}`, {
      method, credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: { authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'content-type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const doc = (await res.json().catch(() => null)) as { error?: { code?: string; message?: string } } | null
    if (!res.ok) throw new ApiError(doc?.error?.message ?? 'Something went wrong. Try again.', doc?.error?.code ?? 'error')
    return doc as T
  }
  return {
    state: () => call<State>('GET', '/state'),
    connect: async (f) => { await call('POST', '/connect', f) },
    workspaces: async () => (await call<{ workspaces: Row[] }>('GET', '/workspaces')).workspaces,
    enrollStart: (code) => call('POST', '/enroll', { code }),
    enrollStatus: (id) => call('GET', `/enroll/${encodeURIComponent(id)}`),
    enrollStop: async (id) => { await call('POST', `/enroll/${encodeURIComponent(id)}/stop`, {}) },
    open: async (workspaceId) => { await call('POST', '/open', { workspace_id: workspaceId }) },
    end: async (workspaceId) => { await call('POST', '/end', { workspace_id: workspaceId }) },
    answer: async (id, approve) => { await call('POST', '/answer', { id, approve }) },
  }
}
