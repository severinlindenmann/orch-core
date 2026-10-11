import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { OrbitMark } from '../brand/OrbitMark'
import { getToken, onSessionChange } from '../api/bootstrap'
import { realApi, type ShellWorkspace } from '../api/realApi'
import './shell.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5000 } } })
type Page = 'today' | 'tickets' | 'identity'
const labels: Record<Page, string> = { today: 'Today', tickets: 'Tickets', identity: 'Identity' }

function Content({ ws, page }: { ws: ShellWorkspace; page: Page }) {
  const [cursor, setCursor] = useState<string | undefined>()
  const identity = useQuery({ queryKey: ['identity', ws.id], queryFn: () => realApi.identity(ws.id), enabled: ws.features.includes('identity') })
  const today = useQuery({ queryKey: ['today', ws.id], queryFn: () => realApi.today(ws.id), enabled: page === 'today' })
  const tickets = useQuery({ queryKey: ['tickets', ws.id, cursor], queryFn: () => realApi.tickets(ws.id, cursor), enabled: page === 'tickets' })
  const active = page === 'today' ? today : page === 'tickets' ? tickets : identity
  if (active.error) return <p role="alert">{active.error.message}</p>
  if (active.isPending) return <p role="status">Loading…</p>
  return <>
    <header className="prod-heading"><h1>{labels[page]}</h1><span>{identity.data?.me.name ?? 'Identity not available'} · Read only</span></header>
    {page === 'today' && <section aria-label="Needs you"><h2>Needs you · {today.data?.needs_you.length}</h2>
      {!today.data?.needs_you.length && <p>Nothing needs your attention.</p>}
      <ul className="prod-rows">{today.data?.needs_you.map((item, i) => <li key={`${item.uid}:${item.ref}:${i}`}>
        <span className="prod-key">{item.ticket ?? 'Workspace'}</span><div><strong>{item.kind.replace('_', ' ')}</strong><p>{item.text || item.ref || 'Details not available'}</p></div>
      </li>)}</ul>
    </section>}
    {page === 'tickets' && <section aria-label="Tickets"><p>{tickets.data?.total} tickets</p><div className="prod-table-scroll"><table>
      <thead><tr><th>Key</th><th>Title</th><th>Status</th><th>Priority</th></tr></thead>
      <tbody>{tickets.data?.rows.map((t) => <tr key={t.uid}><td className="prod-key">{t.key}</td><td>{t.title}</td><td>{t.status}</td><td>{t.priority}</td></tr>)}</tbody>
    </table></div>{tickets.data?.next_page && <button onClick={() => setCursor(tickets.data!.next_page!)}>Next page</button>}
      {cursor && <button onClick={() => setCursor(undefined)}>First page</button>}
    </section>}
    {page === 'identity' && identity.data && <section aria-label="Workspace identity"><dl>
      <dt>Workspace UUID</dt><dd>{identity.data.uuid}</dd><dt>Person</dt><dd>{identity.data.me.person}</dd>
      <dt>Role</dt><dd>{identity.data.me.role}</dd><dt>Genesis</dt><dd>{identity.data.genesis ?? 'Not available'}</dd>
      <dt>Epoch</dt><dd>{identity.data.epoch ?? 'Not available'}</dd><dt>Workspace key fingerprint</dt><dd>{identity.data.key_fingerprint ?? 'Not available'}</dd>
    </dl>{identity.data.tier === 'A' && <p>Agents share your user account.</p>}</section>}
  </>
}

function WorkspaceShell() {
  const list = useQuery({ queryKey: ['workspaces'], queryFn: realApi.workspaces })
  const session = useQuery({ queryKey: ['session'], queryFn: realApi.session })
  const [address, setAddress] = useState(() => location.pathname + location.search)
  const [error, setError] = useState('')
  useEffect(() => { const pop = () => setAddress(location.pathname + location.search); window.addEventListener('popstate', pop); return () => window.removeEventListener('popstate', pop) }, [])
  const url = new URL(address, location.origin)
  const [, , prefix, requested] = url.pathname.split('/')
  const matches = list.data?.filter((w) => (w.prefix === prefix || w.id === prefix)) ?? []
  const suffix = url.searchParams.get('ws')
  const ws = prefix ? matches.find((w) => suffix ? w.id.replaceAll('-', '').startsWith(suffix) : matches.length === 1) : list.data?.[0]
  const features = ws ? session.data?.workspaces[ws.id] ?? [] : []
  const page: Page = requested === 'tickets' || requested === 'identity' ? requested : 'today'
  function go(workspace: ShellWorkspace, target: Page) {
    const duplicate = list.data!.filter((w) => w.prefix === workspace.prefix).length > 1
    const next = `/w/${encodeURIComponent(workspace.prefix || workspace.id)}/${target}${duplicate ? '?ws=' + workspace.id.replaceAll('-', '').slice(0, 8) : ''}`
    history.pushState(null, '', next); setAddress(next)
  }
  return <div className="prod-shell"><aside className="prod-sidebar"><div className="prod-brand"><OrbitMark /><span>orch</span></div>
    <label htmlFor="workspace">Workspace</label><select id="workspace" value={ws?.id ?? ''} onChange={(e) => { const w = list.data!.find((x) => x.id === e.target.value)!; go(w, 'today') }}>
      <option value="" disabled>Choose workspace</option>{list.data?.map((w) => <option key={w.id} value={w.id}>{w.name} · {w.prefix || w.id.slice(0, 8)}{w.host_state !== 'ok' ? ' · host not running' : ''}</option>)}
    </select><nav aria-label="Main">{(['today', 'tickets', 'identity'] as const).filter((p) => features.includes(p)).map((p) => <button key={p} aria-current={page === p ? 'page' : undefined} onClick={() => go(ws!, p)}>{labels[p]}</button>)}</nav>
    <button className="prod-logout" onClick={() => { void realApi.logout().catch((e: Error) => setError(e.message)) }}>Log out</button>
  </aside><main className="prod-main">
    {(list.error || session.error || error) && <p role="alert">{list.error?.message ?? session.error?.message ?? error}</p>}
    {list.isPending && <p role="status">Loading workspaces…</p>}
    {list.data && !ws && <p>{matches.length > 1 ? 'Choose a workspace: this prefix is shared.' : 'Choose an available workspace.'}</p>}
    {ws && ws.host_state !== 'ok' && <p role="alert">{ws.host_state === 'addon.disabled' ? 'Dashboard addon is disabled.' : 'Host not running.'}</p>}
    {ws && ws.host_state === 'ok' && (features.includes(page) ? <Content key={`${ws.id}:${page}`} ws={ws} page={page} /> : <p>Host too old for this feature, or feature not available.</p>)}
  </main></div>
}

export function Shell() {
  const [token, setToken] = useState(getToken)
  useEffect(() => onSessionChange(() => { queryClient.clear(); setToken(getToken()) }), [])
  if (!token) return <main className="prod-login"><OrbitMark /><h1>Open your dashboard</h1><p>Run <code>orch dashboard</code> in your terminal.</p><p>Addon launcher: <code>python -m orch_dashboard open</code></p></main>
  return <WorkspaceShell />
}
export function mount() {
  createRoot(document.getElementById('root')!).render(<QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>)
}
