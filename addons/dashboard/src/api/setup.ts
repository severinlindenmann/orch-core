/** Setup is deliberately independent of dashboard session storage and query caches. */
export interface SetupState {
  phase: 'profile' | 'secrets' | 'review' | 'confirming' | 'running' | 'done' | 'failed' | 'cancelled'
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
      const value = await res.json()
      throw new SetupError(value.error?.code ?? 'setup.unavailable')
    }
    return res.json() as Promise<T>
  }
}
export async function bootstrapSetup(fetcher: typeof fetch = fetch): Promise<SetupAPI> {
  let fragment = new URLSearchParams(location.hash.slice(1))
  let code = fragment.get('launch') ?? fragment.get('code') ?? ''
  history.replaceState(null, '', '/setup') // MUST precede every fetch, including the exchange.
  fragment = new URLSearchParams()
  if (navigator.serviceWorker?.controller) throw new SetupError('setup.service_worker')
  try {
    const res = await fetcher('/auth/setup', {
      method: 'POST',
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })
    if (!res.ok) throw new SetupError('session.required')
    const value = (await res.json()) as { token: string }
    return new SetupAPI(value.token, fetcher)
  } finally {
    code = ''
  }
}
