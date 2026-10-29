import { versionQueries, vLabel } from '@/api/versions'
import { RunnerSwitch } from '@/components/RunnerSwitch'
import { AccessLength } from '../AccessLength'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { DURATIONS, canChangeSettings, inRange, type DurationKey } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { queries } from '@/api/queries'
import { CopyButton } from '../General'
import { visible } from '@/components/sign/visible'
import { DASHBOARD_EXECUTION_LINE } from '@/api/quickRecords'
import { SigningCard } from './SigningCard'
import { HostAlarms } from '@/components/dev/DevHost'
import { Mono, Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerRole } from './useViewerRole'
import { useSettingsHandoff } from './useSettingsHandoff'
import { ReposSection } from './Repos'
import { RecordsBlock } from './Records'
import { WorkspaceNote } from './WorkspaceNote'
import { TrustedBrowsers } from './TrustedBrowsers'
import { WorkspaceIconFor } from '@/app/workspaceIcon'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { WorkspaceIconPicker } from '../WorkspaceIconPicker'
import { useCosmetic } from '../useCosmetic'

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(0,120px)_minmax(0,1fr)] items-center gap-3 py-1.5 text-[13px]">
      <dt className="text-text-muted">{label}</dt>
      <dd className="flex min-w-0 flex-wrap items-center gap-1 break-all">{children}</dd>
    </div>
  )
}

/** Everyday workspace settings, with technical settings and records in Advanced. */
export function HostGeneral({ workspace }: { workspace: Workspace }) {
  const settings = useQuery(queries.hostSettings(workspace.id))
  const identity = useQuery(queries.identity(workspace.id))
  const role = useViewerRole(workspace)
  const hostInfo = useQuery(queries.hostInfo())
  const versions = useQuery(versionQueries.get(workspace.id))
  const { ask, dialog } = useSettingsHandoff(workspace.id)
  const [iconOpen, setIconOpen] = useState(false)
  const [renaming, setRenaming] = useState<string | null>(null)
  const cosmetic = useCosmetic(workspace)
  const [draft, setDraft] = useState<Partial<Record<DurationKey, string>>>({})
  if (settings.isError) return <LoadFailed what="settings" onRetry={() => void settings.refetch()} />
  const id = identity.data
  // The runner is what the host says (`whoami.runner`); nothing is drawn when it says nothing. The list of workspaces carries it too.
  const runner = id?.runner ?? workspace.runner ?? null
  const data = settings.data
  const served = hasFeature('do:settings.set', workspace.id)
  const why = !canChangeSettings(role) ? 'Only owners can save.' : !served ? 'This host cannot change settings yet.' : null

  const value = (k: DurationKey) => draft[k] ?? String(data?.[k] ?? '')
  const changed = DURATIONS.filter((d) => draft[d.key] !== undefined && draft[d.key] !== String(data?.[d.key]))
  const timingChanged = changed.filter(d => d.key !== 'grant_hours')
  const grantChanged = changed.some(d => d.key === 'grant_hours')
  const invalid = timingChanged.find((d) => !inRange(Number(draft[d.key]), d.min, d.max))
  const save = (keys: DurationKey[]) => {
    const set = Object.fromEntries(changed.filter(d => keys.includes(d.key)).map((d) => [d.key, Number(draft[d.key])]))
    ask({
      title: 'Change the workspace settings',
      action: { action: 'settings_set', ...set },

    })
  }
  // D6: the name is one unsigned PUT (owners only); the editor closes once it is saved.
  const rename = async () => {
    if (renaming !== null && (await cosmetic.save({ name: renaming.trim() }))) setRenaming(null)
  }

  return (
    <div className="space-y-4">
      <header className="flex min-w-0 items-start gap-3">
        <div className="flex shrink-0 flex-col items-center gap-1">
          <WorkspaceIconFor workspace={workspace} className="size-11 bg-brand-soft" />
          {workspace.icon_known && <Popover open={iconOpen} onOpenChange={setIconOpen}>
            <PopoverTrigger asChild><button type="button" className="rounded px-1 text-[12px] text-text-muted underline-offset-2 hover:text-text hover:underline">Change</button></PopoverTrigger>
            <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)]">
              <WorkspaceIconPicker workspace={workspace} canEdit={role === 'owner'}
                onSave={(choice) => { setIconOpen(false); void cosmetic.save({ icon: choice }) }} />
            </PopoverContent>
          </Popover>}
        </div>
        <div className="min-w-0 space-y-1 break-words">
          {renaming === null ? (
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="min-w-0 text-[22px] font-semibold leading-tight [overflow-wrap:anywhere]"><bdi>{visible(workspace.name)}</bdi></h2>
              {role === 'owner' && <Button variant="ghost" size="sm" onClick={() => setRenaming(workspace.name)}>Rename</Button>}
            </div>
          ) : (
            <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); void rename() }}>
              <Input aria-label="Workspace name" className="max-w-sm" maxLength={80} autoFocus value={renaming} onChange={(e) => setRenaming(e.target.value)} />
              <Button type="submit" size="sm" disabled={!renaming.trim() || renaming.trim() === workspace.name}>Save</Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setRenaming(null)}>Cancel</Button>
            </form>
          )}
          <p className="text-[13px] text-text-muted">Tickets are numbered {workspace.prefix}-0001, {workspace.prefix}-0002 ...</p>
        </div>
      </header>
      {workspace.click !== undefined && <SigningCard workspace={workspace} owner={role === 'owner'} />}
      <Section title="Agents">
        {!data ? <p className="text-[13px] text-text-muted">Reading the settings…</p> : !data.available ?
          <p data-testid="settings-unavailable" className="text-[13px] text-text-muted">This host does not say what the settings are, so none is shown.</p> : <>
          <AccessLength variant="select" value={Number(value('grant_hours'))} disabled={!!why} onChange={(hours) => setDraft({ ...draft, grant_hours: String(hours) })} />
          {grantChanged && <Button className="mt-3" disabled={!!why} onClick={() => save(['grant_hours'])}>Save</Button>}
          {why && <OwnerNote id="agents-why">{why}</OwnerNote>}
        </>}
      </Section>
      <WorkspaceNote workspace={workspace} />
      <ReposSection workspace={workspace} data={data} onSettled={() => void settings.refetch()} />
      <TrustedBrowsers />
      <details className="rounded-lg border border-border bg-surface text-[13px]">
        <summary className="cursor-pointer px-4 py-3 font-medium">Advanced</summary>
        <div className="space-y-5 p-4 pt-0">
          <div><h4 className="mb-2 font-semibold">Technical details</h4><dl className="divide-y divide-border">
          <Row label="Prefix"><Mono>{workspace.prefix}</Mono><CopyButton value={workspace.prefix} label="prefix" /></Row>
          <Row label="UUID">
            <Mono>{id?.uuid ?? workspace.id}</Mono>
            <CopyButton value={id?.uuid ?? workspace.id} label="UUID" />
          </Row>
          {id?.key_fingerprint && (
            <Row label="Key fingerprint">
              <Mono>{id.key_fingerprint}</Mono>
              <CopyButton value={id.key_fingerprint} label="key fingerprint" />
            </Row>
          )}
          {id?.epoch != null && (
            <Row label="Epoch">
              <span>epoch {id.epoch}</span>
            </Row>
          )}
          </dl></div>
          {versions.data && (versions.data.core.version || versions.data.core.release || versions.data.dashboard.version) && (
            <div><h4 className="mb-2 font-semibold">Versions</h4>
              <dl data-testid="versions" className="divide-y divide-border">
                <Row label="orch core">
                  <span>{versions.data.core.version ? vLabel(versions.data.core.version) : 'version not known'}</span>
                  {versions.data.core.release && <Mono>release {versions.data.core.release}</Mono>}
                </Row>
                <Row label="Dashboard">
                  <span>{versions.data.dashboard.version ? vLabel(versions.data.dashboard.version) : 'version not known'}</span>
                </Row>
              </dl>
              <p className="mt-1 text-[12px] text-text-muted">Each add-on's version is in the Add-ons list. The release is the running host's build; if it differs from your terminal's, restart the host.</p>
            </div>
          )}

          <div><h4 className="mb-2 font-semibold">Signing methods</h4>
          </div>

          <div><h4 className="mb-2 font-semibold">How long things are held</h4>
            {!data ? (
              <p className="text-[13px] text-text-muted">Reading the settings…</p>
            ) : !data.available ? (
              <p className="text-[13px] text-text-muted">
                This host does not say what the settings are, so none is shown.
              </p>
            ) : (
              <>
                <div className="grid gap-4 @[40rem]/page:grid-cols-2">
                  {DURATIONS.filter(d => d.key !== 'grant_hours').map((d) => (
                    <div key={d.key} className="space-y-1.5">
                      <Label htmlFor={`set-${d.key}`}>
                        {d.key === 'claim_ttl_min' ? 'Ticket reserved for (minutes)' : 'Task held for (minutes)'}
                      </Label>
                      <p className="text-[12px] text-text-muted">{d.hint}</p>
                      <Input
                        id={`set-${d.key}`}
                        type="number"
                        min={d.min}
                        max={d.max}
                        step={1}
                        value={value(d.key)}
                        disabled={!!why}
                        aria-invalid={draft[d.key] !== undefined && !inRange(Number(draft[d.key]), d.min, d.max) ? true : undefined}
                        onChange={(e) => setDraft({ ...draft, [d.key]: e.target.value })}
                      />
                      <p className="text-[12px] text-text-faint">
                        {data.defaults?.includes(d.key) ? 'The default. ' : ''}
                        {d.min} to {d.max} {d.unit}
                      </p>
                    </div>
                  ))}
                </div>
                {invalid && (
                  <p role="alert" className="mt-2 text-[13px] text-danger">
                    {invalid.label}: use a whole number from {invalid.min} to {invalid.max} {invalid.unit}.
                  </p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <Button disabled={!!why || timingChanged.length === 0 || !!invalid} onClick={() => save(['claim_ttl_min', 'lease_ttl_min'])} aria-describedby={why ? 'settings-why' : undefined}>
                    Save settings
                  </Button>
                  {why && <OwnerNote id="settings-why">{why}</OwnerNote>}
                </div>

              </>
            )}
          </div>
          {runner && (
            <div><h4 className="mb-2 font-semibold">Dashboard execution</h4>
              <div data-testid="workspace-card-runner" className="space-y-2 text-[13px]">
                {runner.enabled ? (
                  <p data-testid="runner-execution-line" className="text-warning">
                    {DASHBOARD_EXECUTION_LINE}
                  </p>
                ) : (
                  <p className="text-text-muted">
                    Dashboard execution is off for this workspace: no agent or terminal can be started from here. Turn it on below, after you read what it allows.
                  </p>
                )}
                <p className="text-text-muted">
                  {runner.connected ? 'The runner on this machine is connected' : 'The runner on this machine is not connected'}
                  {runner.enabled ? `; terminals are ${runner.terminals ? 'available' : 'not available'}.` : '.'}
                </p>
                {role === 'owner' && (
                  <div className="flex flex-wrap items-start gap-2">
                    <RunnerSwitch ws={workspace.id} on={!runner.enabled} testId="card-runner" variant="secondary" />
                    {runner.enabled && !runner.terminals && <RunnerSwitch ws={workspace.id} on initialTerminals label="Turn on terminals" testId="card-runner-terminals" variant="secondary" />}
                  </div>
                )}
              </div>
            </div>
          )}

          <HostAlarms alarms={hostInfo.data?.alarms ?? []} />
          <RecordsBlock workspace={workspace} />
        </div>
      </details>
      {dialog}
    </div>
  )
}
