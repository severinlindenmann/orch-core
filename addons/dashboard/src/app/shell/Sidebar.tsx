import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Bot, ListChecks, LayoutDashboard, Settings, SquareKanban } from 'lucide-react'
import { api } from '@/api/client'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { Wordmark } from '@/brand/OrbitMark'
import { iconByName } from '../icons'
import { useWorkspace } from '../workspace'

const linkClass =
  'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted transition-colors hover:bg-surface-2 hover:text-text'
const activeProps = { className: 'bg-surface-2 !text-text' }

const CORE_NAV = [
  { to: '/', label: 'Today', icon: LayoutDashboard },
  { to: '/board', label: 'Board', icon: SquareKanban },
  { to: '/tickets', label: 'Tickets', icon: ListChecks },
  { to: '/agents', label: 'Agents', icon: Bot },
] as const

export function Sidebar() {
  const { workspace } = useWorkspace()
  const { data: addons = [] } = useQuery({ queryKey: ['addons'], queryFn: api.getAddons })
  const navItems = addons
    .filter((a) => a.enabled)
    .flatMap((a) => a.contributions.filter((c) => c.slot === 'nav').map((c) => ({ addon: a.name, ...c })))

  return (
    <aside className="flex w-[220px] shrink-0 flex-col border-r border-border bg-sidebar">
      <div className="flex h-12 items-center px-4">
        <Wordmark size={24} />
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="Main">
        {CORE_NAV.map(({ to, label, icon: Icon }) => (
          <Link key={to} to={to} className={linkClass} activeProps={activeProps} activeOptions={{ exact: to === '/' }}>
            <Icon className="size-4" />
            <span className="flex-1">{label}</span>
            {to === '/' && workspace && workspace.needs_you > 0 && (
              <span className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">{workspace.needs_you}</span>
            )}
          </Link>
        ))}

        {navItems.length > 0 && (
          <div className="mt-4">
            <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-text-faint">Addons</div>
            {navItems.map((item) => {
              const Icon = iconByName(item.icon)
              return (
                <Link
                  key={item.id}
                  to="/addon/$name/$id"
                  params={{ name: item.addon, id: item.id }}
                  className={linkClass}
                  activeProps={activeProps}
                >
                  <Icon className="size-4" />
                  <span className="flex-1">{item.title}</span>
                  <AddonBadge name={item.addon} />
                </Link>
              )
            })}
          </div>
        )}
      </nav>
      <div className="border-t border-border p-2">
        <Link to="/settings" className={linkClass} activeProps={activeProps}>
          <Settings className="size-4" />
          Settings
        </Link>
      </div>
    </aside>
  )
}
