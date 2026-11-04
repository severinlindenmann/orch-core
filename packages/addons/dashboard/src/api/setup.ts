/** Setup is deliberately independent of dashboard session storage and query caches. */
export interface SetupState {
  phase: 'profile' | 'secrets' | 'review' | 'confirming' | 'running' | 'done' | 'failed' | 'cancelled'
  error?: { code: string; message: string } | null
  mode: 'new' | 'finish'
  dir: string
  dir_name: string
  /** The computer running setup (its host name); absent in older cores (the page then says "this computer"). */
  host?: string
  suggested: { name: string; workspace_name: string; prefix: string; method?: 'terminal' | 'click'; agent_hours?: number; agent_grant?: boolean }
  addons: {
    id: string
    title: string
    desc: string
    required: boolean
    default: boolean
    kind: 'addon' | 'flag'
    capabilities: string[]
  }[]
  methods: ('terminal' | 'click')[]
  /** The OS of the machine running setup; absent or unknown hides OS-specific notes. */
  os?: 'darwin' | 'linux' | 'windows'
  click_available: boolean
  click_reason: string
  /** Core's convenience-signing risk sentence, shown with the method choice before Continue. */
  click_risk: string
  agent_hours: number[]
  relay: { available: boolean; enabled: boolean }
  /** The folder is (or lies in) a git working copy; absent in older cores (nothing is offered then). */
  git?: { repo: boolean }
  /** Claude Code is installed here: setup offers to let it trust the new folder. */
  claude?: { available: boolean; why_not?: string; parent_config?: string }
  missing: string[]
  /** `missing` in words, same order (core's wording; the page shows these, never the ids) */
  missing_words?: string[]
  /** Finish setup: what the workspace has now (read-only on the page) */
  current?: {
    name: string
    prefix: string
    addons_on: string[]
    agent_hours: number
    runner: { enabled: boolean; terminals: boolean }
    runner_grant: boolean
    method: 'terminal' | 'click'
  } | null
  /** Finish setup on a workspace that lacks nothing: the page offers only "Open dashboard" */
  complete?: boolean
  /** step/item id -> words (`runner_grant`: "Agent access for this computer's runner") */
  labels?: Record<string, string>
}
export interface Profile {
  name: string
  /** The workspace's display name (shown in menus; not signed). */
  workspace_name: string
  prefix: string
  addons: string[]
  method: 'terminal' | 'click'
  agent_hours: number
  agent_grant: boolean
  relay: boolean
  /** Make the folder a git repository (only with Work copies, only where there is none). */
  git_init?: boolean
  /** Keep a records history (own git repository, saved on this computer automatically; never pushed). New workspaces only. */
  records?: boolean
  /** Let Claude Code trust this folder (offered only when the claude CLI is installed). */
  trust_folder?: boolean
  /** "Trust anyway": Claude Code settings in a folder above this one apply too (default off). */
  trust_parent?: boolean
}
export interface Secrets {
  passphrase: string | null
  recovery_words: string[]
}
export interface Review {
  review_id: string
  digest: string
  fingerprint: string[] | null
  /** What a new setup creates, from core's review (bound by its digest): never from page state. */
  workspace?: { prefix: string; name: string; workspace_name?: string; dir: string; lines: string[] } | null
  items: {
    id: string
    title: string
    lines: string[]
    details: string[]
    volatile: string[]
    note?: string
    /** The signed event's type and payload (core's own structure: the summary is built from these, never from prose). */
    type?: string
    fields?: Record<string, unknown>
    /** Positions in `lines` / `details` that are values the host draws at signing (core says which; no text matching). */
    placeholders?: number[]
    placeholder_details?: number[]
  }[]
  runner_grant: { hours: number; scope: string; holder: string } | null
  flags: { id: string; text: string; detail?: string }[]
  risk: { text: string; once: boolean } | null
  tty_confirm: boolean
}
export interface Progress {
  waiting?: string | null
  steps: { id: string; label: string; status: 'todo' | 'doing' | 'done' | 'failed' | 'skipped' }[]
  done: boolean
  error: { code: string; message: string; finishable: boolean; details?: string } | null
  retry: string[]
  reasons?: Record<string, string>
  reason_codes?: Record<string, string>
  workspace: { id: string; prefix: string } | null
}
export class SetupError extends Error {
  constructor(public code: string) {
    super(code)
  }
}
export class SetupAPI {
  constructor(
    private token: string,
    private fetcher: typeof fetch = fetch,
  ) {}
  forget() {
    this.token = ''
  }
  /** Page left after "ready": tell the setup it is finished (best effort, survives the page going away). */
  leave() {
    if (!this.token) return
    const fetcher = this.fetcher
    try {
      void fetcher('/api/setup/finish', {
        method: 'POST',
        credentials: 'omit',
        cache: 'no-store',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Setup-Token': this.token },
        body: '{}',
      }).catch(() => undefined)
    } catch {
      /* nothing to do: the setup ends by its idle timeout */
    }
  }
  /** Page is closing or reloading before "ready": tell the setup so the terminal can offer a fresh link. Same token
   * header as every call (a beacon cannot set it); best effort. Hiding the tab is NOT leaving: only pagehide calls this. */
  left() {
    if (!this.token) return
    const fetcher = this.fetcher
    try {
      void fetcher('/api/setup/left', {
        method: 'POST',
        credentials: 'omit',
        cache: 'no-store',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Setup-Token': this.token },
        body: '{}',
      }).catch(() => undefined)
    } catch {
      /* the setup notices the silence after 150 s */
    }
  }
  async call<T>(path: string, body?: unknown): Promise<T> {
    // Called unbound: `this.fetcher(...)` would run the browser's fetch with `this` = SetupAPI, which real browsers
    // refuse ("Illegal invocation"); a test double never notices.
    const fetcher = this.fetcher
    const res = await fetcher(`/api/setup/${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-Setup-Token': this.token },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    if (!res.ok) {
      const value = await res.json().catch(() => null)
      throw new SetupError(typeof value?.error?.code === 'string' ? value.error.code : `http.${res.status}`)
    }
    return res.json() as Promise<T>
  }
}
export async function bootstrapSetup(fetcher: typeof fetch = fetch): Promise<SetupAPI> {
  // The one-time nonce of the launch link (same exposure as `orch dashboard open`): read from the fragment, which is
  // stripped before any request, and presented once. Without it nothing is claimed.
  const nonce = new URLSearchParams(location.hash.slice(1)).get('launch')
  history.replaceState(null, '', '/setup')
  if (navigator.serviceWorker?.controller) throw new SetupError('setup.service_worker')
  if (!nonce) throw new SetupError('session.required')
  const res = await fetcher('/auth/setup', {
    method: 'POST',
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nonce }),
  })
  if (!res.ok) throw new SetupError('session.required')
  const value = (await res.json()) as { token: string }
  return new SetupAPI(value.token, fetcher)
}

/** The launch link opens the root with `to=/setup` in its fragment (the shared login-link path); a reload is `/setup`. */
export function isSetupPage(): boolean {
  return location.pathname === '/setup' || new URLSearchParams(location.hash.slice(1)).get('to') === '/setup'
}
