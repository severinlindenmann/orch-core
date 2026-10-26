// Settings > Add-ons (R2-2, real host): every add-on with its plain description and whether it is On. A switch is a
// signed settings change like the others: it opens the same review and signing step (`addon.grant` to turn on,
// `addon.disable` to turn off). Nothing is written without a signature. A switch that cannot move says why.
import { Inline, plain } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { AddonSetup } from './AddonSetup'
import { HostDevices } from './Devices'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, ChevronDown, ChevronRight, FolderOpen, GitBranch, Globe, Loader2, Puzzle, Smartphone, Rocket, Ruler, Terminal, Gauge, type LucideIcon } from 'lucide-react'
import { useState } from 'react'
import { canChangeSettings } from '@/api/settings'
import { queries } from '@/api/queries'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Switch } from '@/components/ui/switch'
import { RunnerSwitch } from '@/components/RunnerSwitch'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { PublishServer } from './PublishServer'
import { useSettingsHandoff } from './useSettingsHandoff'
import { useViewerRole } from './useViewerRole'

type Switchable = 'start-agent' | 'terminals' | 'worktrees' | 'publish' | 'usage' | 'estimate' | 'explorer' | 'wiki'
/** Add-ons with settings of their own: they open in place, under their row. */
const WITH_SETTINGS: readonly string[] = ['publish']
const SWITCHABLE: readonly string[] = ['start-agent', 'terminals', 'worktrees', 'publish', 'usage', 'estimate', 'explorer', 'wiki']

const ICONS: Record<string, LucideIcon> = { 'start-agent': Rocket, terminals: Terminal, worktrees: GitBranch, publish: Globe, usage: Gauge, estimate: Ruler, explorer: FolderOpen, wiki: BookOpen }

export function HostAddOns({ workspace }: { workspace: Workspace }) {
  const [setup, setSetup] = useState<{ name: 'publish'; enabling: boolean } | null>(null)
  const data = useQuery(queries.addOns(workspace.id))
  const role = useViewerRole(workspace)
  const qc = useQueryClient()
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [devicesOpen, setDevicesOpen] = useState(false)
  // The row being switched: its switch keeps the old position (nothing is shown before the host confirms) and says what is happening.
  const [busy, setBusy] = useState<{ id: string; turningOn: boolean; stage: 'review' | 'signing' | 'waiting' } | null>(null)
  const { ask, dialog } = useSettingsHandoff(workspace.id, async () => {
    setBusy((b) => (b ? { ...b, stage: 'waiting' } : b))
    // One read of everything the page and the sidebar are built from; the switch moves when these have answered.
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['add-ons', workspace.id] }),
      qc.invalidateQueries({ queryKey: ['workspaces'] }),
      qc.invalidateQueries({ queryKey: ['workspace-addons', workspace.id] }),
      qc.invalidateQueries({ queryKey: queries.hostSettings(workspace.id).queryKey }),
    ])
  })
  const owner = canChangeSettings(role)

  const flip = (row: AddOnRow) => {
    if (!SWITCHABLE.includes(row.id)) return
    const name = row.id as Switchable
    const turningOn = row.state !== 'on'
    if (turningOn && row.needs && row.set_up !== true && name === 'publish') {
      setSetup({ name, enabling: true })
      return
    }
    const name_ = plain(row.title)
    setBusy({ id: row.id, turningOn, stage: 'review' })
    ask({
      title: turningOn ? `Turn on ${name_}` : `Turn off ${name_}`,
      action: { action: turningOn ? 'addon_grant' : 'addon_disable', name },
      autoStart: true,
      confirmLabel: turningOn ? 'Sign and turn on' : 'Sign and turn off',
      doneToast: turningOn ? `${name_} is on` : `${name_} is off`,
      onSigning: (signing) => setBusy((b) => (b && signing ? { ...b, stage: 'signing' } : b)),
      onEnd: () => setBusy(null),
      summary: (
        <p className="text-[13px] text-text-muted">
          {turningOn
            ? `${name_} becomes available to you and your agents in this workspace.`
            : `${name_} is switched off for you and your agents in this workspace. What it already saved stays, and you can switch it back on at any time.`}
        </p>
      ),
    })
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Add-ons</h2>
      <Section title="What this workspace can do">
        <p className="mb-2 text-[13px] text-text-muted">
          Add-ons give you and your agents more to work with. Turning one on or off is a change you sign, like the other settings: you see exactly what changes first.
        </p>
        {data.isError ? (
          <LoadFailed what="add-ons" onRetry={() => void data.refetch()} />
        ) : !data.data ? (
          <p className="text-[13px] text-text-muted">Reading the add-ons…</p>
        ) : !data.data.available ? (
          <p className="text-[13px] text-text-muted">This host does not say which add-ons there are.</p>
        ) : (
          <ul aria-label="Add-ons" className="divide-y divide-border">
            {data.data.add_ons.map((row) => {
              const fixed = row.locked !== null
              const why = fixed ? null : !owner ? 'Only owners can change add-ons.' : row.blocked
              // Off and blocked: greyed. On and blocked (work is running): cannot be turned off now.
              const disabled = fixed || !owner || row.blocked !== null
              const on = row.state === 'on'
              const Icon = ICONS[row.id] ?? Puzzle
              const mine = busy?.id === row.id ? busy : null
              const needsSetup = owner && on && row.needs && row.set_up === false && row.id === 'publish'
              return (
                <li key={row.id} data-testid={`addon-${row.id}`} className="py-3 text-[13px]">
                  <div className="flex items-center gap-3">
                    <Icon aria-hidden className="size-4 shrink-0 text-text-muted" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2">
                        {on && WITH_SETTINGS.includes(row.id) ? (
                          <button
                            type="button"
                            aria-expanded={!!open[row.id]}
                            aria-controls={`addon-${row.id}-panel`}
                            aria-label={`${row.title} settings`}
                            data-testid={`addon-${row.id}-expand`}
                            className="-ml-1 inline-flex items-center gap-1 rounded px-1 font-medium hover:bg-surface-2 focus-visible:outline focus-visible:outline-2"
                            onClick={() => setOpen((o) => ({ ...o, [row.id]: !o[row.id] }))}
                          >
                            {open[row.id] ? <ChevronDown aria-hidden className="size-3.5" /> : <ChevronRight aria-hidden className="size-3.5" />}
                            <Inline>{row.title}</Inline>
                          </button>
                        ) : (
                          <span className="font-medium"><Inline>{row.title}</Inline></span>
                        )}
                        {fixed && <span className="text-[11px] text-text-faint">Always on</span>}
                        {needsSetup && <span data-testid={`addon-${row.id}-state`} className="rounded-full border border-warning/50 px-1.5 text-[11px] leading-4 text-warning">Needs setup</span>}
                      </div>
                      <p className="text-text-muted"><Inline>{row.desc}</Inline></p>
                      {why && <OwnerNote id={`addon-${row.id}-why`}>{why}</OwnerNote>}
                      {owner && !on && row.runner_switch && <div className="mt-1.5"><RunnerSwitch ws={workspace.id} on initialTerminals={row.id === 'terminals'} testId={`addon-${row.id}-runner`} variant="secondary" /></div>}
                      {row.state === 'attention' && !why && (
                        <p className="text-[12px] text-text-muted">Its files changed since you approved it. Turn it on again to approve what it is now.</p>
                      )}
                    </div>
                    {needsSetup && <Button size="sm" variant="secondary" onClick={() => setSetup({ name: 'publish', enabling: false })}>Set up</Button>}
                    {mine && mine.stage !== 'review' && (
                      <span role="status" data-testid={`addon-${row.id}-pending`} className="inline-flex items-center gap-1.5 text-[12px] text-text-muted">
                        <Loader2 className="size-3.5 animate-spin" aria-hidden /> {mine.turningOn ? 'Turning on…' : 'Turning off…'}
                      </span>
                    )}
                    {!fixed && (
                      <Switch
                        aria-label={`${plain(row.title)}`}
                        aria-describedby={why ? `addon-${row.id}-why` : undefined}
                        aria-busy={mine ? true : undefined}
                        checked={on}
                        disabled={disabled || !!busy}
                        onCheckedChange={() => flip(row)}
                      />
                    )}
                  </div>
                  {on && open[row.id] && (
                    <div id={`addon-${row.id}-panel`}>
                      {row.id === 'publish' && <PublishServer workspace={workspace} owner={owner} embedded />}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </Section>
      <Section title="Phone">
        <ul aria-label="Phone">
          <li data-testid="addon-phone" className="py-3 text-[13px]">
            <div className="flex items-center gap-3">
              <Smartphone aria-hidden className="size-4 shrink-0 text-text-muted" />
              <div className="min-w-0 flex-1">
                <span className="font-medium">Phone</span>
                <p className="text-text-muted">Pair the orch app on your phone, and see, rename or remove the devices that can approve.</p>
              </div>
              <Button size="sm" variant="secondary" data-testid="addon-phone-devices" onClick={() => setDevicesOpen(true)}>Manage devices</Button>
            </div>
          </li>
        </ul>
      </Section>
      {devicesOpen && (
        <Dialog open onOpenChange={(o) => !o && setDevicesOpen(false)}>
          <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Phone and devices</DialogTitle>
              <DialogDescription>Pairing, and the devices that can approve for this workspace.</DialogDescription>
            </DialogHeader>
            <HostDevices key={workspace.id} workspace={workspace} />
          </DialogContent>
        </Dialog>
      )}
      {setup && <AddonSetup name={setup.name} enabling={setup.enabling} close={() => setSetup(null)} submit={(title, action) => { setSetup(null); ask({ title, action, autoStart: true }) }} />}
      {dialog}
    </div>
  )
}
