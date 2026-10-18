// Settings > Add-ons (R2-2, real host): every add-on with its plain description and whether it is On. A switch is a
// signed settings change like the others: it opens the same review and signing step (`addon.grant` to turn on,
// `addon.disable` to turn off). Nothing is written without a signature. A switch that cannot move says why.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { canChangeSettings } from '@/api/settings'
import { queries } from '@/api/queries'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Switch } from '@/components/ui/switch'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { ModelRouting } from './ModelRouting'
import { PublishServer } from './PublishServer'
import { useSettingsHandoff } from './useSettingsHandoff'
import { useViewerRole } from './useViewerRole'

const STATE_WORD = { on: 'On', off: 'Off', attention: 'Needs approval again' } as const
type Switchable = 'start-agent' | 'terminals' | 'worktrees' | 'publish' | 'usage' | 'estimate' | 'explorer' | 'model-routing'
const SWITCHABLE: readonly string[] = ['start-agent', 'terminals', 'worktrees', 'publish', 'usage', 'estimate', 'explorer', 'model-routing']

export function HostAddOns({ workspace }: { workspace: Workspace }) {
  const data = useQuery(queries.addOns(workspace.id))
  const role = useViewerRole(workspace)
  const qc = useQueryClient()
  const { ask, dialog } = useSettingsHandoff(workspace.id, () => {
    void qc.invalidateQueries({ queryKey: ['add-ons', workspace.id] })
    void qc.invalidateQueries({ queryKey: ['workspaces'] })
  })
  if (data.isError) return <LoadFailed what="add-ons" onRetry={() => void data.refetch()} />
  const owner = canChangeSettings(role)

  const flip = (row: AddOnRow) => {
    if (!SWITCHABLE.includes(row.id)) return
    const name = row.id as Switchable
    const turningOn = row.state !== 'on'
    ask({
      title: turningOn ? `Turn on ${row.title}` : `Turn off ${row.title}`,
      action: { action: turningOn ? 'addon_grant' : 'addon_disable', name },
      autoStart: true,
      summary: (
        <p className="text-[13px] text-text-muted">
          {turningOn
            ? `${row.title} becomes available to you and your agents in this workspace.`
            : `${row.title} is switched off for you and your agents in this workspace. What it already saved stays, and you can switch it back on at any time. You will type the add-on's name in your terminal to confirm.`}
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
        {!data.data ? (
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
              return (
                <li key={row.id} data-testid={`addon-${row.id}`} className="flex items-start gap-3 py-3 text-[13px]">
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{row.title}</span>
                      <span
                        data-testid={`addon-${row.id}-state`}
                        className={on ? 'rounded-full border border-border px-1.5 text-[11px] leading-4 text-success' : 'rounded-full border border-border px-1.5 text-[11px] leading-4 text-text-muted'}
                      >
                        {fixed ? 'Always on' : STATE_WORD[row.state]}
                      </span>
                    </div>
                    <p className="text-text-muted">{row.desc}</p>
                    {why && <OwnerNote id={`addon-${row.id}-why`}>{why}</OwnerNote>}
                    {row.state === 'attention' && !why && (
                      <p className="text-[12px] text-text-muted">Its files changed since you approved it. Turn it on again to approve what it is now.</p>
                    )}
                  </div>
                  {!fixed && (
                    <Switch
                      aria-label={`${row.title}`}
                      aria-describedby={why ? `addon-${row.id}-why` : undefined}
                      checked={on}
                      disabled={disabled}
                      onCheckedChange={() => flip(row)}
                    />
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </Section>
      {data.data?.add_ons.some((r) => r.id === 'publish' && r.state === 'on') && <PublishServer workspace={workspace} owner={owner} />}
      {data.data?.add_ons.some((r) => r.id === 'model-routing' && r.state === 'on') && <ModelRouting workspace={workspace} owner={owner} />}
      {dialog}
    </div>
  )
}
