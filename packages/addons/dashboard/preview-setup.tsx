import './src/lib/zodJitless'
import './src/lib/cssom'
import './src/styles/tokens.css'
import { createRoot } from 'react-dom/client'
import { Setup, mount } from './src/setup/Setup'
import { SetupAPI, SetupError } from './src/api/setup'
const params = new URLSearchParams(location.search)
// ?remote: the page is opened on another computer than the one that runs setup (P43, needs ?host); the account's full name is suggested (P68; ?noname leaves it empty)
// ?host=NAME: the computer's friendly name (W35; without it the page says "this computer"); the raw host name is only in Details; ?grant: the review issues this computer's runner grant
const host = params.get('host') ?? undefined
const state = {
  phase: 'profile', mode: 'new', dir: '/Users/demo/project', dir_name: 'project', ...(host ? { host } : {}), remote: params.has('remote'), hostname: 'lumes-Virtual-Machine.local',
  suggested: { name: params.has('noname') ? '' : 'Anna Test', workspace_name: params.has('noname') ? 'My workspace' : 'Anna Test’s workspace', prefix: params.has('noname') ? 'MW' : 'AT', method: 'click', agent_hours: 72, agent_grant: true },
  addons: [
    { id: 'dashboard', title: 'Dashboard', desc: 'Browser', required: true, default: true, kind: 'addon', capabilities: [] },
    { id: 'worktrees', title: 'Own copy per ticket', desc: 'Each ticket gets its own copy of your project so agents do not overwrite each other.', required: false, default: true, kind: 'addon', capabilities: [] },
    { id: 'quick', title: 'Quick tasks', desc: 'Small one-off tasks', required: false, default: true, kind: 'flag', capabilities: [] },
  ],
  methods: ['terminal', 'click'], click_available: true, click_reason: '',
  click_risk: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.',
  agent_hours: [8, 24, 72, 168], relay: { available: true, enabled: true }, missing: [],
  labels: { runner_grant: "Agent access for this computer's runner", custody: 'One-click approval on this computer' },
  git: { repo: !params.has('nogit') },
}
const review = {
  review_id: 'r', digest: 'd', fingerprint: ['a', 'b', 'c'],
  items: [
    { id: 'settings', title: 'Workspace settings', lines: ['Change the workspace settings', '', 'Changes', '  grant_hours: 72', '', 'Agents may get access for up to 3 days (members; the runner too).'], details: ['set: {"grant_hours":72}', 'type: settings.changed'], volatile: [], type: 'settings.changed', fields: { set: { grant_hours: 72 } }, placeholders: [], placeholder_details: [] },
    { id: 'addon:dashboard', title: 'Dashboard', lines: ['Turn on Dashboard: it says it will serve web pages from this computer', '', 'Fields', '  add-on: dashboard', '  version: 0.1.0', '', 'An add-on runs its own code on this computer as you; turn it on only if you trust it.'], details: ['artifact_kinds: []', 'capabilities: ["serve_http"]', 'name: dashboard', 'package_sha256: sha256:6e98390f0b1c0a3b5e7d2f9a4c8b1d6e3f5a7c9b2d4e6f8a0b1c3d5e7f9a1b2c', 'type: addon.granted', 'version: 0.1.0'], volatile: [], type: 'addon.granted', fields: { name: 'dashboard', version: '0.1.0', capabilities: ['serve_http'] }, placeholders: [], placeholder_details: [] },
    { id: 'addon:worktrees', title: 'Own copy per ticket', lines: ['Turn on Own copy per ticket', '', 'Fields', '  add-on: worktrees'], details: ['name: worktrees', 'type: addon.granted'], volatile: [], type: 'addon.granted', fields: { name: 'worktrees', capabilities: [] }, placeholders: [], placeholder_details: [] },
    // the item the server applies when agents may start on this computer (P44/P69: Review reads it, not the profile)
    ...(params.has('grant') ? [{ id: 'runner_grant', title: "Agent access for this computer's runner", lines: [], details: [], volatile: [], type: 'grant.issued', fields: { hours: 72, scope: 'workable', verbs: 'agent' }, placeholders: [], placeholder_details: [] }] : []),
  ],
  runner_grant: params.has('grant') ? { hours: 72, scope: 'workable', holder: "this machine's runner, never shown" } : null,
  flags: [
    { id: 'custody', text: 'Approvals on this computer need only one click. Anything running as you could approve too.' },
    { id: 'records', text: "Keep a history of your tickets on this computer, saved automatically (never uploaded)." },
  ],
  risk: { text: 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.' },
  tty_confirm: params.has('grant'),
  workspace: { prefix: 'AT', name: 'Anna Test', workspace_name: 'Anna Test’s workspace', dir: '/Users/demo/project', lines: ['Workspace AT for Anna Test', 'Folder: /Users/demo/project'] },
}
const fetcher = async (url: RequestInfo | URL, init?: RequestInit) => {
  const path = String(url).replace('/api/setup/', '')
  const body = init?.body ? JSON.parse(String(init.body)) : undefined
  let r: unknown = { ok: true }
  if (path === 'state') r = state
  if (path === 'secrets/reveal') r = { passphrase: null, recovery_words: Array.from({ length: 24 }, (_, i) => `word-${i + 1}`) }
  if (path === 'secrets/confirm') r = { ok: true }
  if (path === 'review') r = review
  if (path === 'create') r = { run_id: 'x' }
  if (path.startsWith('progress')) {
    const ids = [['keys','Creating your key'],['genesis','Saving your approval'],['register','Starting orch in the background'],['git','Preparing work copies'],['runner','Letting the dashboard run agents'],['settings','Saving the workspace settings'],['addon:quick','Quick tasks'],['flags','Turning on one-click approval'],['runner_grant','Letting agents start on this computer'],['custody','Storing your key for one-click approval']]
    r = { done: true, error: null, retry: [], waiting: null, steps: ids.map(([id,label]) => ({ id, label, status: 'done' })) }
  }
  void body
  return new Response(JSON.stringify(r), { status: 200 })
}
if (params.has('used')) void mount(Promise.reject(new SetupError('session.required')))
else createRoot(document.getElementById('root')!).render(<Setup api={new SetupAPI('t', fetcher as typeof fetch)} />)
