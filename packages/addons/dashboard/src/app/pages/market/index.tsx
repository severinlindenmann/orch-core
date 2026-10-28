import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { api } from '@/api/client'
import { canAct, hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import {
  marketError,
  permissionEffect,
  publisherWording,
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
import { InfoDrawer } from '@/components/InfoDrawer'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useWorkspace } from '@/app/workspace'
import { useRole } from '@/app/useRole'
import { BuiltInAddOns } from '../settings/host/AddOns'
import { InstalledGroup } from './InstalledGroup'
import { RemoveSheet } from './RemoveSheet'
import { AddMarketplace } from './AddMarketplace'
import { SettingsSheet } from './SettingsSheet'

function InstallSetup({ stage, cancel, proceed }: { stage: MarketStage; cancel: () => void; proceed: (values: Record<string, unknown>) => void }) {
  const required = stage.settings_schema?.required ?? []
  const props = stage.settings_schema?.properties ?? {}
  const [values, setValues] = useState<Record<string, unknown>>(() => Object.fromEntries(required.filter(k => props[k]?.default !== undefined).map(k => [k, props[k].default])))
  return <section aria-label="Install add-on" className="space-y-3 border-t border-border py-3">
    <p><Inline>{stage.name}</Inline> · Version <Inline>{stage.version}</Inline> · Publisher <Inline>{stage.market.publisher.fingerprint ?? 'unsigned'}</Inline></p>
    {required.map(key => <label key={key} className="block"><Inline>{key}</Inline>
      {props[key]?.type === 'boolean'
        ? <input type="checkbox" checked={values[key] === true} onChange={e => setValues(v => ({ ...v, [key]: e.target.checked }))} />
        : <Input aria-label={key} value={typeof values[key] === 'object' ? JSON.stringify(values[key]) : String(values[key] ?? '')} onChange={e => {
          const text = e.target.value
          let value: unknown = text
          if (props[key]?.type === 'integer') value = text === '' ? undefined : Number(text)
          if (props[key]?.type === 'object' || props[key]?.type === 'array') { try { value = JSON.parse(text) } catch { value = text } }
          setValues(v => ({ ...v, [key]: value }))
        }} />}
    </label>)}
    <InfoDrawer><p>Package digest <Raw>{stage.package_sha256}</Raw></p></InfoDrawer>
    <Button variant="ghost" onClick={cancel}>Cancel</Button>
    <Button disabled={stage.expires_at * 1000 <= Date.now() || required.some(k => values[k] === undefined)} onClick={() => proceed(values)}>Install and turn on</Button>
  </section>
}

function BuiltInGroup({ ws }: { ws: NonNullable<ReturnType<typeof useWorkspace>['workspace']> }) {
  return (
    <section aria-labelledby="built-in-h" className="space-y-1">
      <h2 id="built-in-h" className="text-sm font-semibold">Built in</h2>
      <BuiltInAddOns key={ws.id} workspace={ws} />
    </section>
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
  const market = !!ws && hasFeature('market', ws)
  const mayWrite = !!ws && canAct(ws) && role === 'owner' && market
  const { tab: asked } = useSearch({ from: '/addons' })
  // A host without the market ops has the built-in add-ons only.
  const tab = market ? asked : 'installed'
  const navigate = useNavigate()
  const qc = useQueryClient()
  const list = useQuery({ ...queries.marketList(ws), enabled: market })
  const installed = useQuery({ ...queries.marketInstalled(ws), enabled: market && tab === 'installed' })
  const [search, setSearch] = useState('')
  const [source, setSource] = useState('')
  const [permission, setPermission] = useState('')
  const [stage, setStage] = useState<MarketStage | null>(null)
  const [installSettings, setInstallSettings] = useState<Record<string, unknown>>({})
  const [handoff, setHandoff] = useState<MarketStage | null>(null)
  const [pending, setPending] = useState<string[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [remove, setRemove] = useState<InstalledAddon | null>(null)
  const [settingsOf, setSettingsOf] = useState<string | null>(null)
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
      if ('up_to_date' in result) {
        setMessage('Already up to date')
        setStage(null)
      } else {
        setStage(result)
        if (!(result.settings_schema?.required?.length)) {
          setInstallSettings({})
          setHandoff(result)
        }
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
  return (
    <div className="@container min-w-0 w-full max-w-5xl space-y-4 break-words">
      <h1 className="text-xl font-semibold">Add-ons</h1>
      <nav aria-label="Add-on tabs" className="flex gap-2 border-b border-border pb-2">
        {(market ? (['browse', 'installed', 'marketplaces'] as const) : (['installed'] as const)).map((t) => (
          <Button key={t} size="sm" variant={tab === t ? 'secondary' : 'ghost'} onClick={() => void navigate({ to: '/addons', search: { tab: t } })}>
            {t === 'browse' ? 'Browse' : t === 'installed' ? 'Installed' : 'Marketplaces'}
          </Button>
        ))}
      </nav>
      {message && <p role="status">{message}</p>}
      {!market ? (
        workspace && <><BuiltInGroup ws={workspace} /><h2 className="pt-2 text-sm font-semibold">From marketplaces</h2><p className="text-[13px] text-text-muted">Nothing installed from a marketplace yet.</p></>
      ) : active.isError ? (
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
                        <Inline>{`${m.class === 'official' ? 'official - ' : ''}${m.url.replace(/^https:\/\/github\.com\//, '')}`}</Inline>
                      </p>
                      <InfoDrawer><p>Version <Inline>{a.version}</Inline></p></InfoDrawer>
                      <AddonFrame addon={a.id} title="From the addon" compact className="flex-1 items-start text-sm">
                        <Inline>{a.from_marketplace.summary}</Inline>
                      </AddonFrame>
                      <p className="text-[11px] text-text-faint">The text above comes from the add-on package: not verified.</p>
                      <div className="flex flex-wrap gap-1">
                        {[...new Set(a.capabilities)].map((p) => (
                          <span className="rounded-full border border-border px-2 text-xs" key={p}>
                            <Inline>{permissionEffect(p)}</Inline>
                          </span>
                        ))}
                      </div>
                      <p>{a.revoked ? 'Withdrawn by its publisher' : a.blocked ? 'Blocked by your hub' : stateLabel(a.installed?.state, pending.includes(a.id))}</p>
                      {a.installed && <p className="text-xs text-text-muted">Installed{a.installed.state === 'disabled' ? ' · Turn it on under Installed' : ''}</p>}
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
              {workspace && <BuiltInGroup ws={workspace} />}
              <h2 className="pt-2 text-sm font-semibold">From marketplaces</h2>
              {workspace && (
                <InstalledGroup
                  workspace={workspace}
                  addons={installed.data?.addons ?? []}
                  summaryOf={(name) => entries.find(({ a }) => a.id === name)?.a.from_marketplace.summary}
                  mayWrite={mayWrite}
                  busy={busy}
                  onUpdate={(a) => prepare({ id: a.name, update: true })}
                  onSettings={(a) => setSettingsOf(a.name)}
                  onRemove={a => {
                    if (a.state === 'active') { setRemove(a); return }
                    void write(async () => { await api.marketUninstall(ws, a.name); await refresh(); setMessage(`${a.name} removed. Install it again from Browse.`) })
                  }}
                />
              )}
            </>
          )}
          {tab === 'marketplaces' && (
            <>
              {!markets.length ? (
                <p>No marketplaces added yet</p>
              ) : (
                <ul aria-label="Marketplaces" className="space-y-2">
                  {markets.map((m) => {
                    const name = m.from_marketplace.title || m.from_marketplace.name
                    const repo = m.url.replace(/^https:\/\/github\.com\//, '')
                    return (
                      <li key={m.url} className="rounded-lg border border-border bg-surface p-4">
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                          <div className="min-w-0 flex-1 basis-64">
                            <div className="flex min-w-0 items-center gap-2">
                              <span className="min-w-0 max-w-full font-medium" title={visible(name)}>
                                <AddonFrame addon="marketplace" title="From the marketplace" compact>
                                  <Inline>{name}</Inline>
                                </AddonFrame>
                              </span>
                              {m.cache === 'invalid' ? <span className="shrink-0 text-xs text-warning">Needs refresh</span> : m.cache === 'not_fetched' ? <span className="shrink-0 text-xs text-text-muted">Not read yet</span> : m.stale && <span className="shrink-0 text-xs text-warning">Out of date - refresh</span>}
                              {m.class === 'official' && (
                                <a
                                  href={m.url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  title="Open on GitHub"
                                  className="inline-flex shrink-0 items-center gap-1 text-xs text-text-muted hover:text-text"
                                >
                                  <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                                    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
                                  </svg>
                                  GitHub
                                </a>
                              )}
                            </div>
                            <div className="flex min-w-0 flex-wrap items-center gap-x-3 text-xs text-text-muted">
                              <span className="min-w-0 max-w-full truncate font-mono" title={visible(repo)}>
                                <Inline>{repo}</Inline>
                              </span>
                              <span>{m.entries} {m.entries === 1 ? 'add-on' : 'add-ons'}</span>
                              <span>{m.publisher_key ? publisherWording(m.publisher_key) : m.class === 'official' && m.verified ? 'Signed by orch' : 'Unsigned'}</span>
                              <span>{m.last_fetch ? `Refreshed ${new Date(m.last_fetch * 1000).toLocaleString()}` : 'Never refreshed'}</span>
                            </div>
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
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
                            <InfoDrawer label="About this marketplace">
                              <p>Allowed by: <Inline>{m.allowed_by ?? 'Not reported'}</Inline></p>
                              <p>Added by: <Inline>{m.added_by ?? 'Not reported'}</Inline></p>
                              <p className="text-text-muted">Shown by this computer, not covered by your approval.</p>
                            </InfoDrawer>
                          </div>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}
              {mayWrite && <AddMarketplace ws={ws} list={list.data} markets={markets} onAdded={() => setMessage('Marketplace added. Its add-ons are in Browse.')} />}
            </>
          )}
        </>
      )}
      {mayWrite && stage && !handoff && <InstallSetup key={stage.stage} stage={stage} cancel={cancel} proceed={values => { setInstallSettings(values); setHandoff(stage) }} />}
      {mayWrite && handoff && (
        <SignPopover
          title="Install and turn on add-on"
          inline
          ws={ws}
          action={{ action: 'market_grant', name: handoff.name, staged: handoff.stage, grant: handoff.grant, settings: installSettings }}
          lead={
            <div data-testid="install-summary" className="rounded border border-border p-3 text-sm">
              <p className="font-medium">
                <Inline>{handoff.name}</Inline> · Version <Inline>{handoff.version}</Inline> · Publisher <Inline>{handoff.market.publisher.fingerprint ?? 'unsigned'}</Inline>
              </p>
              <p>Installs and turns on after all signatures have landed.</p>
              <InfoDrawer><p>Package digest <Raw>{handoff.package_sha256}</Raw></p></InfoDrawer>
              <p className="text-text-muted">The marketplace lines below are shown by this computer, are not signed and are not verified. Your approval does not cover them.</p>
              {widened(handoff) && <p role="alert" className="font-medium text-warning">This update asks for more than before.</p>}
              {handoff.market.stale && <p className="text-warning">This review is out of date: the marketplace may have changed. Cancel and review again.</p>}
              {handoff.market.class === 'dev' && <p className="text-warning">This comes from a test marketplace (dev sandbox only).</p>}
            </div>
          }
          onClose={() => {
            void cancel()
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
      {mayWrite && workspace && settingsOf && <SettingsSheet workspace={workspace} name={settingsOf} onClose={() => setSettingsOf(null)} />}
      {mayWrite && remove && <RemoveSheet ws={ws} addon={remove} onClose={() => setRemove(null)} onRemoved={(name) => { setRemove(null); setMessage(`${name} removed. Install it again from Browse.`) }} />}
    </div>
  )
}
