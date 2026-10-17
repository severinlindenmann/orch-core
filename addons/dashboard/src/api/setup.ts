/** Setup is deliberately independent of dashboard session storage and query caches. */
export interface SetupState {
  phase: 'profile' | 'secrets' | 'review' | 'confirming' | 'running' | 'done' | 'failed' | 'cancelled'
  error?: { code: string; message: string } | null
  mode: 'new' | 'finish'
  dir: string
  dir_name: string
  suggested: { name: string; prefix: string }
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
  click_available: boolean
  click_reason: string
  agent_hours: number[]
  relay: { available: boolean; enabled: boolean }
  missing: string[]
}
export interface Profile {
  name: string
  prefix: string
  addons: string[]
  method: 'terminal' | 'click'
  agent_hours: number
  agent_grant: boolean
  relay: boolean
}
export interface Secrets {
  passphrase: string | null
  recovery_words: string[]
  check_positions: number[]
}
export interface Review {
  review_id: string
  digest: string
  fingerprint: string[] | null
  items: { id: string; title: string; lines: string[]; details: string[]; volatile: string[]; note?: string }[]
  runner_grant: { hours: number; scope: string; holder: string } | null
  flags: { id: string; text: string }[]
  risk: { text: string; once: boolean } | null
  typed_word: string | null
  tty_confirm: boolean
}
export interface Progress {
  waiting?: string | null
  steps: { id: string; label: string; status: 'todo' | 'doing' | 'done' | 'failed' | 'skipped' }[]
  done: boolean
  error: { code: string; message: string; finishable: boolean } | null
  retry: string[]
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
  async call<T>(path: string, body?: unknown): Promise<T> {
    const res = await this.fetcher(`/api/setup/${path}`, {
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
