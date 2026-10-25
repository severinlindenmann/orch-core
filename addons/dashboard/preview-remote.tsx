// Dev-only preview of the remote dashboard screens (#1198 R1-8): ?screen=login | login-failed | login-locked |
// enroll | enroll-expired | enroll-unsupported | settings | settings-one | settings-problem | stepup | stepup-waiting |
// stepup-cancelled | stepup-expired | terminal. The server and the device are faked here; nothing leaves the page.
import './src/lib/zodJitless'
import './src/lib/cssom'
import './src/styles/tokens.css'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { setFeatures } from './src/api/features'
import { setEnrollCode } from './src/api/enrollLink'
import { noteStepUpRefused, stepUp, toBase64Url } from './src/api/passkey'
import { CLICK_RISK } from './src/api/click'
import { EnrollPage } from './src/app/pages/login/EnrollPage'
import { LoginGate } from './src/app/pages/login/LoginPage'
import { RemoteAccess } from './src/app/pages/settings/host/RemoteAccess'
import { PreviewDialog } from './src/app/terminal/real/PreviewDialog'
import { ClickSignPanel } from './src/components/sign/ClickSignPanel'
import { TooltipProvider } from './src/components/ui/tooltip'

const screen = new URLSearchParams(location.search).get('screen') ?? 'login'
const json = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status })
const opts = { challenge_id: 'c'.repeat(22), publicKey: { challenge: toBase64Url(new Uint8Array(32)), rpId: location.hostname, allowCredentials: [], userVerification: 'required' } }
const mac = { id: 'mac', name: 'Safari on Mac', created: '2026-10-03T10:00:00Z', last_used: new Date().toISOString() }
const phone = { id: 'phone', name: 'Safari on iPhone', created: '2026-10-03T10:00:00Z', last_used: '2026-10-24T10:00:00Z' }
const keys = screen === 'settings-one' ? [mac] : [mac, phone]
const setCreds = (get: () => Promise<unknown>) => Object.defineProperty(navigator, 'credentials', { configurable: true, value: { get, create: get } })
const never = () => new Promise<never>(() => {})
const cancelled = () => Promise.reject(new DOMException('x', 'NotAllowedError'))
;(window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential ??= class {}
window.fetch = (async (input: string) => {
  if (input.endsWith('/auth/passkey/options')) return json(screen === 'login-locked' ? 429 : 200, opts)
  if (input.endsWith('/auth/enroll/options')) return json(screen === 'enroll-expired' ? 401 : 200, { challenge_id: 'd'.repeat(22), publicKey: { ...opts.publicKey, rp: { id: 'x', name: 'orch' }, user: { id: 'AA', name: 'orch', displayName: 'orch' }, pubKeyCredParams: [] } })
  if (input.endsWith('/api/passkeys')) return json(200, { passkeys: keys })
  if (input.endsWith('/api/stepup/options') || input.includes('/stepup/options')) return json(200, { ...opts, link_id: 'l'.repeat(22) })
  if (input.endsWith('/enroll-link')) return json(200, { link: `https://${location.hostname}/#enroll=k7Q9vH2mx4Lp9aaaaaaaaaaa`, expires_in: 872 })
  return json(404)
}) as typeof fetch
setCreds(screen === 'login-failed' ? () => Promise.reject(new Error('x')) : screen === 'login' || screen === 'enroll' || screen === 'enroll-unsupported' ? never : screen === 'stepup-cancelled' ? cancelled : screen === 'stepup-waiting' ? never : cancelled)
if (screen === 'enroll-unsupported') (window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential = undefined
if (screen.startsWith('enroll')) setEnrollCode('k7Q9vH2mx4Lp9aaaaaaaaaaa')
setFeatures({ features: [], login: screen.startsWith('stepup') ? 'remote' : 'local', remote_address: `https://${location.hostname}`, ...(screen === 'settings-problem' ? { remote_problem: 'front not answering since 14:02' } : {}) })
if (screen === 'settings-problem') setFeatures({ features: [], login: 'local', remote_problem: 'front not answering since 14:02' })

const view = { view_digest: 'sha256:' + 'a'.repeat(64), view: { risk: CLICK_RISK, op: 'access', workspace: { id: 'w'.repeat(32), name: 'marketing-site', prefix: 'MKT' }, fingerprint: 'abcdef012345', headline: 'Turn on One click', lines: ['What: Agents on "marketing-site" may sign the steps One click covers without asking you each time', 'For: Workspace "marketing-site"', 'Until: You turn it off'], checks: [], confirm: { kind: 'click' as const }, expires_at: '2026-10-12T12:10:00Z', auth: 'convenience', device: 'd_x', artifacts: null, seal: { v: 1, alg: 'x', purposes: ['grant.secret'] } } }
if (screen.startsWith('stepup') && screen !== 'stepup') {
  if (screen === 'stepup-expired') noteStepUpRefused({ code: 'passkey.stepup_required' })
  else void stepUp({ action: 'device.add' }).catch(() => {})
}
const tp = { spec: 'f'.repeat(64), display: 'claude --resume', display_hidden: false, cwd_path: '/work/marketing-site', cwd_path_hidden: false, notice: null, as_person: false, execution_line: 'This terminal runs as orch-agent-sev. It cannot read your keys. Never type secrets into it.', login_shell: false, env: [], blocked: null, warnings: [] }
const qc = new QueryClient()
const page = (() => {
  if (screen.startsWith('login')) return <LoginGate local={<p>local</p>} />
  if (screen.startsWith('enroll')) return <EnrollPage />
  if (screen.startsWith('settings')) return <div className="mx-auto max-w-3xl p-4"><RemoteAccess /></div>
  if (screen === 'terminal') return <PreviewDialog preview={{ data: tp, error: '' }} busy={false} onClose={() => {}} onConfirm={() => {}} />
  return <div className="mx-auto max-w-lg p-4"><ClickSignPanel answer={view} problem={null} signing={false} onSign={() => void stepUp({ action: 'device.add' })} onCancel={() => {}} /></div>
})()
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={qc}><TooltipProvider><div className="min-h-dvh bg-bg text-text">{page}</div></TooltipProvider></QueryClientProvider>)
