import './src/lib/zodJitless'
import './src/lib/cssom'
import './src/styles/tokens.css'
import { createRoot } from 'react-dom/client'
import { Setup } from './src/setup/Setup'
import { SetupAPI } from './src/api/setup'
const state = {
  phase: 'profile', mode: 'new', dir: '/Users/demo/project', dir_name: 'project',
  suggested: { name: 'Demo', prefix: 'TST', method: 'click', agent_hours: 72, agent_grant: true },
  addons: [
    { id: 'dashboard', title: 'Dashboard', desc: 'Browser', required: true, default: true, kind: 'addon', capabilities: [] },
    { id: 'quick', title: 'Quick tasks', desc: 'Small one-off tasks', required: false, default: true, kind: 'flag', capabilities: [] },
  ],
  methods: ['terminal', 'click'], click_available: true, click_reason: '',
  click_risk: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.',
  agent_hours: [8, 24, 72, 168], relay: { available: true, enabled: true }, missing: [],
  labels: { runner_grant: "Agent access for this Mac's runner", custody: 'One-click approval on this Mac' },
  git: { repo: true },
}
const review = {
  review_id: 'r', digest: 'd', fingerprint: ['a', 'b', 'c'],
  items: [{ id: 'settings', title: 'Settings', lines: [], details: [], volatile: [], type: 'settings.changed', fields: { set: { grant_hours: 72, sign_methods: ['click'] } }, placeholders: [], placeholder_details: [] }],
  runner_grant: null,
  flags: [
    { id: 'custody', text: 'Approvals on this Mac need only one click. Anything running as you could approve too.' },
    { id: 'records', text: "Keep a history of your tickets on this computer, saved automatically (never uploaded)." },
  ],
  risk: { text: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.' },
  typed_word: null, tty_confirm: false,
  workspace: { prefix: 'TST', name: 'Demo', dir: '/Users/demo/project', lines: ['Workspace TST for Demo', 'Folder: /Users/demo/project'] },
}
const fetcher = async (url: RequestInfo | URL, init?: RequestInit) => {
  const path = String(url).replace('/api/setup/', '')
  const body = init?.body ? JSON.parse(String(init.body)) : undefined
  let r: unknown = { ok: true }
  if (path === 'state') r = state
  if (path === 'secrets/reveal') r = { passphrase: null, recovery_words: Array.from({ length: 24 }, (_, i) => `word-${i + 1}`), check_positions: [8, 17, 23] }
  if (path === 'secrets/confirm') r = { ok: true }
  if (path === 'review') r = review
  void body
  return new Response(JSON.stringify(r), { status: 200 })
}
createRoot(document.getElementById('root')!).render(<Setup api={new SetupAPI('t', fetcher as typeof fetch)} />)
