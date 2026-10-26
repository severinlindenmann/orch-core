import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { hasFeature } from '@/api/features'
import { addonActive } from '@/api/addons'
import { queries } from '@/api/queries'
import { acState } from '../../ticket/AcceptanceTasks'
import { Empty, usePanelTicket } from './data'

export function useContextPages(ticket?: string) {
  const detail = usePanelTicket(ticket)
  const enabled = !!ticket && !!detail.workspace && hasFeature('wiki', detail.workspace.id) && addonActive(detail.workspace, 'wiki')
  const query = useQuery({ ...queries.addonState(detail.workspace?.id ?? '', 'wiki', ticket), enabled })
  const byTicket = query.data?.byTicket as Record<string, { title: string }[]> | undefined
  return { ...query, pages: enabled ? byTicket?.[ticket!] ?? [] : [] }
}
export function Context({ ticket }: { ticket?: string }) {
  const { data, isError } = usePanelTicket(ticket)
  const wiki = useContextPages(ticket)
  if (!ticket) return <Empty>No ticket yet. Context will appear here.</Empty>
  if (isError) return <Empty>Could not load context.</Empty>
  if (!data) return <Empty>Loading context…</Empty>
  return <div data-testid="focus-panel-context" className="space-y-5 text-sm">
    <h2 className="font-medium">{data.title}</h2>
    <p className="whitespace-pre-wrap break-words text-text-muted">{data.body.summary || data.body.context || 'No description yet.'}</p>
    <section><h3 className="mb-2 font-medium">Acceptance checks</h3>{!data.acceptance_state.length && <p className="text-text-muted">No acceptance checks yet.</p>}<ul className="divide-y divide-border">{data.acceptance_state.map(ac => <li key={ac.id} className="flex gap-2 py-2"><span aria-label={acState(data, ac) === 'done' ? 'Done' : 'Pending'} className="text-brand">{acState(data, ac) === 'done' ? '✓' : '○'}</span><span>{ac.text}</span></li>)}</ul></section>
    {wiki.pages.length > 0 && <section><h3 className="mb-2 font-medium">Linked wiki pages</h3><ul className="space-y-2">{wiki.pages.map((p, i) => <li key={i}><Link to="/addon/$name/$page" params={{ name: 'wiki', page: 'pages' }} className="text-brand underline">{p.title}</Link></li>)}</ul></section>}
    {wiki.isError && <p className="text-text-muted">Could not load linked wiki pages.</p>}
    <section><h3 className="mb-2 font-medium">Branch</h3><p className="break-all font-mono text-xs text-text-muted">{data.branch.name || 'No branch yet.'}</p></section>
  </div>
}
