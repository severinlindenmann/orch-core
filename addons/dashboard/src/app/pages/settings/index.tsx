import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { api } from '@/api/client'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useSlot } from '@/addon-ui/slots'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../../workspace'
import { usePageHeader } from '../../shell/ShellUi'
import { AddonSettings } from './AddonSettings'
import { General } from './General'
import { AddonManager } from './addons'
import { Gates } from './Gates'
import { Members } from './Members'

export const ONLY_OWNERS = 'Only owners change settings.'

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members & roles' },
  { id: 'gates', label: 'Gate policies' },
  { id: 'addons', label: 'Addons' },
] as const

const link = 'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted hover:bg-surface-2 hover:text-text'
const active = 'bg-surface-2 text-text'

/** /settings/$tab (general | members | gates | addons) and /settings/addon/$name. */
export function SettingsPage({ tab, addon }: { tab?: string; addon?: string }) {
  usePageHeader('Settings')
  const { workspace } = useWorkspace()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const contributions = useSlot('settings')
  const addons = contributions.filter((c, i) => contributions.findIndex((x) => x.addon === c.addon) === i)

  if (!workspace || !me.data) {
    return (
      <div className="space-y-4" aria-busy="true">
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <Skeleton className="h-40 w-full max-w-3xl" />
      </div>
    )
  }
  const isOwner = workspace.members.find((m) => m.person === me.data.person)?.role === 'owner'
  const current = addon ? `addon/${addon}` : (tab ?? 'general')

  return (
    <div className="flex max-w-5xl gap-8">
      <nav aria-label="Settings" className="w-48 shrink-0 space-y-0.5">
        <h1 className="mb-2 px-2.5 text-xl font-semibold tracking-tight">Settings</h1>
        {TABS.map((t) => (
          <Link key={t.id} to="/settings/$tab" params={{ tab: t.id }} className={cn(link, current === t.id && active)} aria-current={current === t.id ? 'page' : undefined}>
            {t.label}
          </Link>
        ))}
        {addons.map((c) => (
          <Link key={c.addon} to="/settings/addon/$name" params={{ name: c.addon }} className={cn(link, current === `addon/${c.addon}` && active)} aria-current={current === `addon/${c.addon}` ? 'page' : undefined}>
            <span className="flex-1 truncate">{c.title}</span>
            <AddonBadge name={c.addon} />
          </Link>
        ))}
      </nav>

      <div className="min-w-0 flex-1 space-y-4">
        {!isOwner && <p className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">{ONLY_OWNERS}</p>}
        {current === 'general' && <General workspace={workspace} canEdit={isOwner} />}
        {current === 'members' && <Members workspace={workspace} viewer={me.data.person} canEdit={isOwner} />}
        {current === 'gates' && <Gates workspace={workspace} canEdit={isOwner} />}
        {current === 'addons' && <AddonManager workspace={workspace} canEdit={isOwner} />}
        {addon && <AddonSettings name={addon} workspace={workspace} canEdit={isOwner} />}
      </div>
    </div>
  )
}
