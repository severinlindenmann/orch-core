import { HubTab } from './hub/HubTab'
import { CompanyTab } from './company/CompanyTab'
import { Link } from '@tanstack/react-router'
import { can, roleOf } from '@/api/permissions'
import { SettingsSkeleton } from '../skeletons'
import { LoadFailed } from '@/components/LoadFailed'
import { useLoadFailure } from '../../useLoadFailure'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { General } from './General'
import { AddonManager } from './addons'
import { Gates } from './Gates'
import { Members } from './Members'
import { Relay } from './relay'
import { Skills } from './Skills'
import { Connections } from './Connections'
import { settingsTabs } from './tabs'
import { isRealHost } from '@/api/features'
import { HostGates } from './host/Gates'
import { HostGeneral } from './host/General'
import { HostDevices } from './host/Devices'
import { HostGrants } from './host/Grants'
import { HostMembers } from './host/Members'
import { CopyLinkButton } from '../../shell/CopyLinkButton'
import { useMe } from '@/app/useMe'
import { Smartphone } from 'lucide-react'
import type { Workspace } from '@/api/types'

export const ONLY_OWNERS = 'Only owners change settings.'

const link = 'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted hover:bg-surface-2 hover:text-text'
const active = 'bg-surface-2 text-text'

/** Settings > Devices on a real host: the Phone block (it used to sit under Add-ons; a phone is not an add-on). */
function HostPhone({ workspace }: { workspace: Workspace }) {
  return (
    <div className="space-y-4">
      <section aria-label="Phone" data-testid="addon-phone" className="space-y-1">
        <div className="flex items-center gap-3 text-[13px]">
          <Smartphone aria-hidden className="size-4 shrink-0 text-text-muted" />
          <div className="min-w-0 flex-1">
            <span className="font-medium">Phone</span>
            <p className="text-text-muted">Pair the orch app on your phone, and see, rename or remove the devices that can approve.</p>
          </div>
        </div>
      </section>
      <HostDevices workspace={workspace} />
    </div>
  )
}

/** /settings/$tab (general | members | gates | relay | addons | skills | connections) and /settings/addon/$name (Addons with that addon's settings drawer open). */
export function SettingsPage({ tab, addon }: { tab?: string; addon?: string }) {
  usePageHeader('Settings')
  const { workspace } = useWorkspace()
  const me = useMe()

  const failure = useLoadFailure()
  if (failure.failed) return <LoadFailed what="settings" onRetry={failure.retry} />
  if (!workspace || !me.data) {
    return <SettingsSkeleton inPage />
  }
  const isOwner = can(roleOf(workspace, me.data.person), 'settings')
  if (isRealHost()) {
    // The reduced settings of a real host (M4): what the host says, and hand-offs for every change.
    return (
      <SettingsFrame current={tab ?? 'general'} addon={undefined} ws={workspace.id}>
        {(tab ?? 'general') === 'general' && <HostGeneral workspace={workspace} />}
        {tab === 'members' && <HostMembers workspace={workspace} />}
        {tab === 'gates' && <HostGates workspace={workspace} />}
        {tab === 'grants' && <HostGrants workspace={workspace} />}
        {tab === 'devices' && <HostPhone key={workspace.id} workspace={workspace} />}
        {tab === 'hub' && <HubTab key={workspace.id} workspace={workspace} />}
        {tab === 'company' && <CompanyTab key={workspace.id} workspace={workspace} isOwner={isOwner} />}
      </SettingsFrame>
    )
  }
  // An addon's own settings page belongs to Addons: that stays the marked section.
  const current = addon ? 'addons' : (tab ?? 'general')

  return (
    <SettingsFrame current={current} addon={addon} ws={workspace.id}>
      {!isOwner && <p className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">{ONLY_OWNERS}</p>}
      {current === 'hub' && <HubTab key={workspace.id} workspace={workspace} />}
      {current === 'company' && <CompanyTab key={workspace.id} workspace={workspace} isOwner={isOwner} />}
      {current === 'general' && <General workspace={workspace} canEdit={isOwner} />}
      {current === 'members' && <Members workspace={workspace} viewer={me.data.person} canEdit={isOwner} />}
      {current === 'gates' && <Gates workspace={workspace} canEdit={isOwner} />}
      {current === 'relay' && <Relay workspace={workspace} canEdit={isOwner} viewer={me.data.person} />}
      {current === 'addons' && <AddonManager workspace={workspace} canEdit={isOwner} settingsOf={addon} />}
      {current === 'skills' && <Skills workspace={workspace} canEdit={isOwner} />}
      {current === 'connections' && <Connections workspace={workspace} canEdit={isOwner} />}
    </SettingsFrame>
  )
}

/** The sub-navigation and the content column, the same for the mockup's settings and the reduced ones of a real host. */
export function SettingsFrame({ current, addon, ws, tabs, children }: { current: string; addon: string | undefined; ws: string; tabs?: readonly { id: string; label: string; preview?: boolean }[]; children: React.ReactNode }) {
  return (
    // A narrow page area (the terminal docked on the right): the sections sit in a row above the content (N11).
    <div className="flex max-w-5xl flex-col gap-4 @[60rem]/page:flex-row @[60rem]/page:gap-8">
      <nav aria-label="Settings" className="flex flex-wrap items-center gap-0.5 @[60rem]/page:block @[60rem]/page:w-48 @[60rem]/page:shrink-0 @[60rem]/page:space-y-0.5">
        <div className="mb-1 flex w-full items-center gap-1 @[60rem]/page:mb-2 @[60rem]/page:px-2.5">
          <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
          <CopyLinkButton label={addon ? `Copy link to the ${addon} settings` : 'Copy link to this settings page'} />
        </div>
        {(tabs ?? settingsTabs(ws)).map((t) => (
          <Link key={t.id} to="/settings/$tab" params={{ tab: t.id }} className={cn(link, current === t.id && active)} aria-current={current === t.id ? 'page' : undefined}>
            <span className="flex-1">{t.label}</span>
            {t.preview && <span className="rounded-full border border-border px-1.5 py-px text-[10px] font-medium leading-4 text-text-muted">Preview</span>}
          </Link>
        ))}
      </nav>
      <div className="min-w-0 flex-1 space-y-4">{children}</div>
    </div>
  )
}
