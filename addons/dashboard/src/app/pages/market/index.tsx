import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { api } from '@/api/client'
import { canAct, hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import {
  EXTERNAL_HELP,
  marketError,
  permissionEffect,
  publisherWording,
  reviewLines,
  stateLabel,
  widened,
  type MarketStage,
  type StageInput,
  type InstalledAddon,
} from '@/api/market'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { Inline, Raw, visible } from '@/components/sign/visible'
import { SignPopover } from '@/components/sign/SignPopover'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useWorkspace } from '@/app/workspace'
import { useRole } from '@/app/useRole'

const HELP = 'Next, review and approve the installation with one click. The add-on will be installed turned off.'
const cell = 'p-2 align-top break-words @max-[800px]:block @max-[800px]:before:block @max-[800px]:before:text-text-faint @max-[800px]:before:content-[attr(data-label)]'
function Table({ columns, children }: { columns: string[]; children: ReactNode }) {
  return (
    <table className="w-full table-fixed text-left text-sm @max-[800px]:block">
      <thead className="text-xs text-text-muted @max-[800px]:sr-only">
        <tr>
          {columns.map((c) => (
            <th className="p-2" key={c}>
              {c}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-border @max-[800px]:block">{children}</tbody>
    </table>
  )
}
function Review({
  stage: s,
  busy,
  error,
  cancel,
  proceed,
  again,
}: {
  stage: MarketStage
  busy: boolean
  error: string
  cancel: () => void
  proceed: () => void
  again: () => void
}) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const expired = Date.parse(s.expires_at) <= now
  // The host's review lines wrap manifest text (title, command, agents text, needs text): all of it is the add-on's, so
  // the whole list sits in one frame (the provenance rule is about where the bytes come from, not who composed the line).
  const full = (
    <AddonFrame addon={s.name} title="From the add-on package (not verified)">
      <ul aria-label="Full review" className="space-y-2">
        {reviewLines(s).map((line, i) => (
          <li key={i} className={line.added ? 'border-l-2 border-success pl-3 text-success' : ''}>
            <Inline>{line.text}</Inline>
          </li>
        ))}
      </ul>
    </AddonFrame>
  )
  return (
    <Dialog open onOpenChange={(open) => !open && !busy && cancel()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto break-words border-border bg-surface">
        <DialogHeader>
          <DialogTitle>
            {s.update ? 'Update' : 'Install'} <Inline>{s.name}</Inline> <Inline>{s.version}</Inline>?
          </DialogTitle>
          <DialogDescription>Approval applies to this exact package; changed code needs new approval.</DialogDescription>
        </DialogHeader>
        {widened(s) && <p role="alert" className="rounded border border-warning p-3 font-medium">This update asks for more than before.</p>}
        {!s.update || widened(s) ? full : null}
        {s.update && (
          <section aria-label="Update changes">
            <p>
              <Inline>{s.update.from.version}</Inline> → <Inline>{s.version}</Inline>
            </p>
            <AddonFrame addon={s.name} title="From the addon" compact className="flex-col items-start">
              {s.update.changes.filter((line) => !/^version\b/i.test(line)).map((line, i) => (
                <p key={i}>
                  <Inline>{line}</Inline>
                </p>
              ))}
              {(['added', 'removed'] as const).map((kind) =>
                Object.entries(s.update![kind]).map(([group, values]) =>
                  values.map((value) => (
                    <p key={`${kind}-${group}-${value}`} className={kind === 'added' ? 'text-success' : 'line-through text-text-muted'}>
                      {kind === 'added' ? '+' : '-'} {group}: <Inline>{group === 'capabilities' ? permissionEffect(value) : value}</Inline>
                    </p>
                  )),
                ),
              )}
            </AddonFrame>
            <p>Publisher: {s.update.publisher === 'same' ? publisherWording('known') : s.update.publisher === 'first_use' ? publisherWording('new') : 'Unsigned'}</p>
          </section>
        )}
        {s.update && !widened(s) && (
          <details>
            <summary>Show full review</summary>
            {full}
          </details>
        )}
        <p>
          from <Inline>{s.market.url}</Inline> <span className="rounded border border-border px-2 text-xs">{s.market.class}</span>
        </p>
        {s.market.stale && <p className="text-warning">Out of date - refresh</p>}
        {s.market.summary && (
          <AddonFrame addon={s.name} title="From the addon">
            <p className="text-xs text-text-muted">Marketplace details (shown by this computer, not signed)</p>
            <Inline>{s.market.summary}</Inline>
          </AddonFrame>
        )}
        {s.deps && (
          <p>
            Also installs {s.deps.count} required software {s.deps.count === 1 ? 'component' : 'components'}. {s.deps.verified ? 'Checked against the add-on\u2019s lock file.' : 'Not checked against a lock file.'}
          </p>
        )}
        <p>
          Package digest: <Inline>{s.package_sha256.slice(0, 12)}</Inline>… (full value in Technical details)
        </p>
        <p>Publisher: {publisherWording(s.market.publisher.tofu)}</p>
        <p>A signature does not prove the code is safe.</p>
        <details>
          <summary>Technical details</summary>
          <p>
            Package digest <Raw>{s.package_sha256}</Raw>
          </p>
          <p>
            Source commit <Raw>{s.market.commit}</Raw>
          </p>
          <p>
            Publisher key <Raw>{s.market.publisher.fingerprint ?? 'unsigned'}</Raw>
          </p>
          <p>
            Stage <Raw>{s.stage}</Raw>
          </p>
          <p>
            Source note <Inline>{s.market.source_note}</Inline>
          </p>
        </details>
        <p>{HELP}</p>
        {error && <p role="alert">{error}</p>}
        {expired && <p role="status">This review expired. Review again before approving.</p>}
        <DialogFooter className="sticky -bottom-6 -mx-6 -mb-6 border-t border-border bg-surface px-6 py-4">
          <Button variant="ghost" disabled={busy} onClick={cancel}>
            Cancel
          </Button>
          {expired ? (
            <Button disabled={busy} onClick={again}>
              Review again
            </Button>
          ) : (
            <Button disabled={busy} onClick={proceed}>
              Review installation
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
export function MarketPage() {
  const { workspace } = useWorkspace()
  return <MarketWorkspace key={workspace?.id ?? 'loading'} />
}
function MarketWorkspace() {
  usePageHeader('Add-ons')
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const role = useRole()
  const mayWrite = !!ws && canAct(ws) && role === 'owner' && hasFeature('market', ws)
  const { tab } = useSearch({ from: '/addons' })
  const navigate = useNavigate()
  const qc = useQueryClient()
  const list = useQuery({ ...queries.marketList(ws), enabled: !!ws && hasFeature('market', ws) })
  const installed = useQuery({ ...queries.marketInstalled(ws), enabled: !!ws && hasFeature('market', ws) && tab === 'installed' })
  const [search, setSearch] = useState('')
  const [source, setSource] = useState('')
  const [permission, setPermission] = useState('')
  const [stage, setStage] = useState<MarketStage | null>(null)
  const [intent, setIntent] = useState<StageInput | null>(null)
  const [handoff, setHandoff] = useState<MarketStage | null>(null)
  const [pending, setPending] = useState<string[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [remove, setRemove] = useState<InstalledAddon | null>(null)
  const refresh = async () => {
    await Promise.all([qc.invalidateQueries({ queryKey: queries.marketList(ws).queryKey }), qc.invalidateQueries({ queryKey: queries.marketInstalled(ws).queryKey })])
  }
  const write = async (task: () => Promise<void>) => {
    if (!mayWrite || busy) return
    setBusy(true)
    setMessage('')
    try {
      await task()
    } catch (e) {
      setMessage(marketError(e))
    } finally {
      setBusy(false)
    }
  }
  const prepare = (input: StageInput) =>
    void write(async () => {
      const result = await api.marketStage(ws, input)
      setIntent(input)
      if ('up_to_date' in result) {
        setMessage('Already up to date')
        setStage(null)
      } else {
        setStage(result)
        setPending((p) => [...p, result.name])
      }
    })
  const cancel = () =>
    void write(async () => {
      if (stage) await api.marketUnstage(ws, stage.stage)
      setPending((p) => p.filter((n) => n !== stage?.name))
      setStage(null)
    })
  const markets = list.data?.markets ?? []
  // The official marketplace is in every workspace: its first fetch happens on the first visit of this page.
  const unfetched = markets.find((m) => m.preset && m.cache === 'not_fetched')?.url
  const fetching = useRef(false)
  useEffect(() => {
    if (!unfetched || fetching.current) return
    fetching.current = true
    api
      .marketRefresh(ws, { url: unfetched })
      .then(() => refresh())
      .catch(() => setMessage('We could not reach the official marketplace. Press Refresh to try again.'))
      .finally(() => {
        fetching.current = false
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unfetched, ws])
  const entries = markets.flatMap((m) => m.addons.map((a) => ({ m, a })))
  const active = tab === 'installed' ? installed : list
  if (!hasFeature('market', ws)) return <p>Add-ons are not available in this workspace.</p>
  return (
    <div className="@container min-w-0 w-full max-w-5xl space-y-4 break-words">
      <h1 className="text-xl font-semibold">Add-ons</h1>
      <nav aria-label="Add-on tabs" className="flex gap-1 border-b border-border pb-2 @max-[520px]:gap-0">
        {(['browse', 'installed', 'marketplaces'] as const).map((t) => (
          <Button key={t} size="sm" variant={tab === t ? 'secondary' : 'ghost'} onClick={() => void navigate({ to: '/addons', search: { tab: t } })}>
            {t === 'browse' ? 'Browse' : t === 'installed' ? 'Installed' : 'Marketplaces'}
          </Button>
        ))}
      </nav>
      {message && <p role="status">{message}</p>}
      {active.isError ? (
        <LoadFailed what={tab === 'installed' ? 'installed add-ons' : 'marketplaces'} onRetry={() => void active.refetch()} />
      ) : active.isPending ? (
        <p role="status">Loading add-ons…</p>
      ) : (
        <>
          {tab === 'browse' && (
            <>
              <div className="grid grid-cols-2 gap-2 @min-[640px]:flex">
                <Input aria-label="Search add-ons" placeholder="Search add-ons…" className="col-span-2 min-w-0 flex-1 basis-48" value={search} onChange={(e) => setSearch(e.target.value)} />
                <select aria-label="Source" className="min-w-0 max-w-full rounded border border-border bg-surface p-2 text-sm" value={source} onChange={(e) => setSource(e.target.value)}>
                  <option value="">All sources</option>
                  {markets.map((m) => (
                    <option key={m.url} value={m.url}>
                      {visible(m.url.replace(/^https:\/\/github\.com\//, ''))}
                    </option>
                  ))}
                </select>
                <select
                  aria-label="Permission"
                  className="min-w-0 max-w-full rounded border border-border bg-surface p-2 text-sm"
                  value={permission}
                  onChange={(e) => setPermission(e.target.value)}
                >
                  <option value="">Any permission</option>
                  {[...new Set(entries.flatMap(({ a }) => a.capabilities))].map((p) => (
                    <option key={p} value={p}>
                      {visible(permissionEffect(p))}
                    </option>
                  ))}
                </select>
              </div>
              {!markets.length && <p>No marketplaces added yet</p>}
              {unfetched && !message && <p role="status">Reading the official marketplace…</p>}
              <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,240px),1fr))] gap-3">
                {entries
                  .filter(
                    ({ m, a }) =>
                      (!source || source === m.url) &&
                      (!permission || a.capabilities.includes(permission)) &&
                      `${a.id} ${a.from_marketplace.summary}`.toLowerCase().includes(search.toLowerCase()),
                  )
                  .map(({ m, a }) => (
                    <article key={`${m.url}/${a.id}`} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4" aria-label={a.id}>
                      <div className="flex items-center gap-3">
                        <span className="rounded bg-surface-2 p-3 text-text-muted">
                          <Inline>{a.id.slice(0, 1).toUpperCase()}</Inline>
                        </span>
                        <h2 className="font-semibold">
                          <Inline>{a.id}</Inline>
                        </h2>
                      </div>
                      <p className="text-xs text-text-muted">
                        <Inline>{`${m.class === 'official' ? 'official - ' : ''}${m.url.replace(/^https:\/\/github\.com\//, '')} - ${a.version}`}</Inline>
                      </p>
                      {m.class === 'official' && <p className="text-xs">Official marketplace</p>}
                      <AddonFrame addon={a.id} title="From the addon" compact className="flex-1 items-start text-sm">
                        <Inline>{a.from_marketplace.summary}</Inline>
                      </AddonFrame>
                      <p className="text-[11px] text-text-faint">The text above comes from the add-on package: not verified.</p>
                      <div className="flex flex-wrap gap-1">
                        {a.capabilities.map((p) => (
                          <span className="rounded-full border border-border px-2 text-xs" key={p}>
                            <Inline>{permissionEffect(p)}</Inline>
                          </span>
                        ))}
                      </div>
                      <p>{a.revoked ? 'Withdrawn by its publisher' : a.blocked ? 'Blocked by your hub' : stateLabel(a.installed?.state, pending.includes(a.id))}</p>
                      {a.installed && <p className="text-xs text-text-muted">Installed{a.installed.state === 'disabled' ? ' · Turn on in Settings > Add-ons' : ''}</p>}
                      {mayWrite &&
                        !a.revoked &&
                        (a.blocked ? (
                          <Button disabled>Ask an admin</Button>
                        ) : (
                          !a.installed &&
                          !pending.includes(a.id) && (
                            <Button disabled={busy} onClick={() => prepare({ id: a.id, url: m.url })}>
                              Install...
                            </Button>
                          )
                        ))}
                    </article>
                  ))}
              </div>
            </>
          )}
          {tab === 'installed' && (
            <>
              {!installed.data?.addons.length ? (
                <p>No add-ons installed yet</p>
              ) : (
                <Table columns={['Add-on', 'Source', 'Version', 'May', 'Actions']}>
                  {installed.data.addons.map((a) => (
                    <tr key={a.name} className="@max-[800px]:block @max-[800px]:py-3">
                      <td className={cell} data-label="Add-on">
                        <Inline>{a.name}</Inline>
                        <p>{stateLabel(a.state)}</p>
                      </td>
                      <td className={cell} data-label="Source">
                        <Inline>{a.origin ?? 'Built in'}</Inline>
                      </td>
                      <td className={cell} data-label="Version">
                        <Inline>{a.version}</Inline>
                        {a.available && (
                          <p>
                            <Inline>{a.available.version}</Inline> ready
                          </p>
                        )}
                      </td>
                      <td className={cell} data-label="May">
                        {entries
                          .find(({ a: entry }) => entry.id === a.name)
                          ?.a.capabilities.map((p) => (
                            <p key={p}>
                              <Inline>{permissionEffect(p)}</Inline>
                            </p>
                          )) ?? 'See package review'}
                      </td>
                      <td className={cell} data-label="Actions">
                        {mayWrite && a.origin && (
                          <div className="flex flex-wrap gap-2">
                            {a.available && !a.available.revoked && (
                              <Button disabled={busy} onClick={() => prepare({ id: a.name, update: true })}>
                                Update...
                              </Button>
                            )}
                            <Button variant="ghost" disabled={busy || a.state !== 'disabled'} onClick={() => setRemove(a)}>
                              Remove
                            </Button>
                            {a.state !== 'disabled' && <p>Turn it off in Settings &gt; Add-ons first</p>}
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </Table>
              )}
            </>
          )}
          {tab === 'marketplaces' && (
            <>
              {!markets.length ? (
                <p>No marketplaces added yet</p>
              ) : (
                <Table columns={['Marketplace', 'Repo', 'Allowed by', 'Added by', 'Publisher signature', 'Add-ons', 'Last refreshed', 'Actions']}>
                  {markets.map((m) => (
                    <tr key={m.url} className="@max-[800px]:block @max-[800px]:py-3">
                      <td className={cell} data-label="Marketplace">
                        <AddonFrame addon="marketplace" title="From the marketplace" compact>
                          <Inline>{m.from_marketplace.title || m.from_marketplace.name}</Inline>
                        </AddonFrame>
                        {m.class === 'official' && (
                          <a
                            href={m.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title="Open on GitHub"
                            className="ml-2 inline-flex items-center gap-1 text-xs text-text-muted hover:text-text"
                          >
                            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
                            </svg>
                            GitHub
                          </a>
                        )}
                        {m.cache === 'invalid' ? <p>Needs refresh</p> : m.cache === 'not_fetched' ? <p>Not read yet</p> : m.stale && <p>Out of date - refresh</p>}
                      </td>
                      <td className={`${cell} font-mono text-xs`} data-label="Repo">
                        <Inline>{m.url.replace(/^https:\/\/github\.com\//, '')}</Inline>
                      </td>
                      <td className={cell} data-label="Allowed by">
                        <Inline>{m.allowed_by ?? 'Not reported'}</Inline>
                      </td>
                      <td className={cell} data-label="Added by">
                        <Inline>{m.added_by ?? 'Not reported'}</Inline>
                      </td>
                      <td className={cell} data-label="Publisher signature">
                        {m.publisher_key ? publisherWording(m.publisher_key) : m.class === 'official' && m.verified ? 'Signed by orch' : 'Unsigned'}
                      </td>
                      <td className={cell} data-label="Add-ons">
                        {m.entries}
                      </td>
                      <td className={cell} data-label="Last refreshed">
                        <Inline>{m.last_fetch ? new Date(m.last_fetch * 1000).toLocaleString() : 'Never'}</Inline>
                      </td>
                      <td className={cell} data-label="Actions">
                        {mayWrite && (
                          <Button
                            disabled={busy}
                            variant="ghost"
                            onClick={() =>
                              void write(async () => {
                                await api.marketRefresh(ws, { url: m.url })
                                await refresh()
                              })
                            }
                          >
                            Refresh
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </Table>
              )}
              <p className="text-xs text-text-muted">Allowed by, added by and publisher signature: shown by this computer, not covered by your approval.</p>
              <p>{EXTERNAL_HELP}</p>
              {mayWrite && (
                <div className="space-y-1">
                  <Button disabled aria-describedby="add-market-why">
                    + Add marketplace
                  </Button>
                  <p id="add-market-why" className="text-xs text-text-muted">
                    Not available: {EXTERNAL_HELP.charAt(0).toLowerCase() + EXTERNAL_HELP.slice(1)}.
                  </p>
                </div>
              )}
            </>
          )}
        </>
      )}
      {mayWrite && stage && !handoff && (
        <Review
          stage={stage}
          busy={busy}
          error={message}
          cancel={cancel}
          again={() =>
            intent &&
            void write(async () => {
              await api.marketUnstage(ws, stage.stage)
              setStage(null)
              const result = await api.marketStage(ws, intent)
              if ('up_to_date' in result) setMessage('Already up to date')
              else setStage(result)
            })
          }
          proceed={() => {
            setHandoff(stage)
          }}
        />
      )}
      {mayWrite && handoff && (
        <SignPopover
          title="Approve add-on package"
          ws={ws}
          anchor={null} // the review dialog is folded away while this one is open (Cancel here brings it back)
          action={{ action: 'market_grant', name: handoff.name, staged: handoff.stage }}
          autoStart
          lead={
            <div data-testid="install-summary" className="rounded border border-border p-3 text-sm">
              <p className="font-medium">
                <Inline>{handoff.name}</Inline> · Version <Inline>{handoff.version}</Inline>
              </p>
              <p>After installation: turned off. You turn it on later in Settings.</p>
              <p className="text-text-muted">The marketplace lines below are shown by this computer, are not signed and are not verified. Your approval does not cover them.</p>
              {widened(handoff) && <p role="alert" className="font-medium text-warning">This update asks for more than before.</p>}
              {handoff.market.stale && <p className="text-warning">This review is out of date: the marketplace may have changed. Cancel and review again.</p>}
              {handoff.market.class === 'dev' && <p className="text-warning">This comes from a test marketplace (dev sandbox only).</p>}
            </div>
          }
          onClose={() => {
            setPending((p) => p.filter((n) => n !== handoff.name))
            setHandoff(null)
          }}
          onSettled={(phase) => {
            if (['done', 'expired', 'cancelled', 'failed'].includes(phase)) {
              setPending((p) => p.filter((n) => n !== handoff.name))
              if (phase === 'done') {
                setStage(null)
                setHandoff(null)
              }
              void refresh()
            }
          }}
        />
      )}
      {mayWrite && remove && (
        <Dialog open onOpenChange={(open) => !open && setRemove(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Remove add-on?</DialogTitle>
              <DialogDescription>Its data stays until you purge it.</DialogDescription>
            </DialogHeader>
            <Inline>{remove.name}</Inline>
            {message && <p role="alert">{message}</p>}
            <DialogFooter className="sticky -bottom-6 -mx-6 -mb-6 border-t border-border bg-surface px-6 py-4">
              <Button variant="ghost" onClick={() => setRemove(null)}>
                Cancel
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  void write(async () => {
                    await api.marketUninstall(ws, remove.name)
                    setRemove(null)
                    await refresh()
                  })
                }
              >
                Remove
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
