import type { ReactNode } from 'react'
import { Link } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, Check, ChevronsUpDown, LayoutDashboard, ListChecks, Settings, ShieldCheck, ShieldOff, SquareKanban } from 'lucide-react'
import { api } from '@/api/client'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useSlot } from '@/addon-ui/slots'
import { OrbitMark } from '@/brand/OrbitMark'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { iconByName } from '../icons'
import { useWorkspace } from '../workspace'

const linkClass =
  'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted transition-colors hover:bg-surface-2 hover:text-text max-xl:justify-center max-xl:px-0 max-xl:py-2'
const activeProps = { className: 'bg-surface-2 !text-text' }

const CORE_NAV = [
  { to: '/', label: 'Today', icon: LayoutDashboard },
  { to: '/board', label: 'Board', icon: SquareKanban },
  { to: '/tickets', label: 'Tickets', icon: ListChecks },
  { to: '/agents', label: 'Agents', icon: Bot },
] as const

/** Below 1280 px the sidebar is an icon rail: the label is hidden and a tooltip carries it. */
function RailTip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side="right" className="xl:hidden">
        {label}
      </TooltipContent>
    </Tooltip>
  )
}

const PEOPLE = [
  { id: 'p_sev', name: 'Severin' },
  { id: 'p_mara', name: 'Mara' },
  { id: 'p_tom', name: 'Tom' },
]

export function Sidebar() {
  const { workspace, workspaces, setWorkspaceId } = useWorkspace()
  const qc = useQueryClient()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const navItems = useSlot('nav')
  const role = workspace?.members.find((m) => m.person === me?.person)?.role ?? me?.role
  const grantTime = me?.grant?.until.slice(11, 16)

  return (
    <aside className="flex w-14 shrink-0 flex-col border-r border-border bg-sidebar xl:w-[232px]">
      <div className="flex h-12 items-center gap-2 px-3.5 max-xl:justify-center max-xl:px-0">
        <OrbitMark size={24} />
        <span className="font-bold tracking-[-0.02em] text-text max-xl:hidden" style={{ fontSize: 18 }}>
          orch
        </span>
      </div>

      <div className="px-2 pb-1">
        <DropdownMenu>
          <RailTip label={workspace ? `${workspace.prefix} · ${workspace.name}` : 'Workspace'}>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label="Switch workspace"
                className="flex w-full items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-left text-[13px] hover:bg-surface-2 max-xl:justify-center max-xl:px-0"
              >
                <span className="rounded bg-surface-3 px-1 font-mono text-[10px] font-semibold text-text-muted">{workspace?.prefix ?? '…'}</span>
                <span className="min-w-0 flex-1 truncate max-xl:hidden">{workspace?.name}</span>
                <ChevronsUpDown className="size-3.5 text-text-faint max-xl:hidden" />
              </button>
            </DropdownMenuTrigger>
          </RailTip>
          <DropdownMenuContent align="start" side="bottom" className="w-60">
            <DropdownMenuLabel className="text-[11px] uppercase tracking-wider text-text-faint">Workspaces</DropdownMenuLabel>
            {workspaces.map((w) => (
              <DropdownMenuItem key={w.id} onSelect={() => setWorkspaceId(w.id)} className="gap-2">
                <span className="w-8 font-mono text-[11px] text-text-faint">{w.prefix}</span>
                <span className="flex-1 truncate">{w.name}</span>
                {w.needs_you > 0 && (
                  <span aria-label={`${w.needs_you} need you`} className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">
                    {w.needs_you}
                  </span>
                )}
                {w.id === workspace?.id && <Check className="size-3.5 text-text-muted" />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="Main">
        {CORE_NAV.map(({ to, label, icon: Icon }) => (
          <RailTip key={to} label={label}>
            <Link to={to} className={linkClass} activeProps={activeProps} activeOptions={{ exact: to === '/' }} aria-label={label}>
              <Icon className="size-4" />
              <span className="flex-1 max-xl:hidden">{label}</span>
              {to === '/' && workspace && workspace.needs_you > 0 && (
                <span className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand max-xl:hidden">{workspace.needs_you}</span>
              )}
            </Link>
          </RailTip>
        ))}

        {navItems.length > 0 && (
          <div className="mt-4">
            <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-text-faint max-xl:hidden">Addons</div>
            <div className="mx-2 mb-1 border-t border-border xl:hidden" />
            {navItems.map((item) => {
              const Icon = iconByName(item.icon)
              return (
                <RailTip key={`${item.addon}/${item.id}`} label={`${item.title} (addon: ${item.addon})`}>
                  <Link
                    to="/addon/$name/$page"
                    params={{ name: item.addon, page: item.id }}
                    className={linkClass}
                    activeProps={activeProps}
                    aria-label={item.title}
                  >
                    <Icon className="size-4" />
                    <span className="flex-1 truncate max-xl:hidden">{item.title}</span>
                    <AddonBadge name={item.addon} className="max-xl:hidden" />
                  </Link>
                </RailTip>
              )
            })}
          </div>
        )}
      </nav>

      <div className="space-y-1 border-t border-border p-2">
        <RailTip label="Settings">
          <Link to="/settings" className={linkClass} activeProps={activeProps} aria-label="Settings">
            <Settings className="size-4" />
            <span className="max-xl:hidden">Settings</span>
          </Link>
        </RailTip>

        <RailTip label={me?.grant ? `Agents granted until ${grantTime}` : 'No grant · run orch grant'}>
          <div
            className={cn(
              'flex items-center gap-2 px-2.5 py-1 text-[11px] max-xl:justify-center max-xl:px-0',
              me?.grant ? 'text-brand' : 'text-warning',
            )}
          >
            {me?.grant ? <ShieldCheck className="size-3.5 shrink-0" /> : <ShieldOff className="size-3.5 shrink-0" />}
            <span className="max-xl:hidden">{me?.grant ? `agents granted until ${grantTime}` : 'no grant · run orch grant'}</span>
          </div>
        </RailTip>

        <DropdownMenu>
          <RailTip label={me ? `${me.name} (${role})` : 'Viewer'}>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label="Viewing as"
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-2 max-xl:justify-center max-xl:px-0"
              >
                <Avatar className="size-7">
                  <AvatarFallback className="bg-surface-3 text-[11px]">{me?.name.slice(0, 2).toUpperCase()}</AvatarFallback>
                </Avatar>
                <span className="min-w-0 flex-1 max-xl:hidden">
                  <span className="block truncate text-[13px] text-text">{me?.name}</span>
                  <span className="block truncate text-[11px] text-text-faint">
                    {role} · viewing as <ChevronsUpDown className="inline size-3" />
                  </span>
                </span>
              </button>
            </DropdownMenuTrigger>
          </RailTip>
          <DropdownMenuContent align="start" side="top" className="w-48">
            <DropdownMenuLabel className="text-[11px] uppercase tracking-wider text-text-faint">Viewing as (dev)</DropdownMenuLabel>
            <DropdownMenuRadioGroup
              value={me?.person}
              onValueChange={async (person) => {
                await api.setViewer(person)
                await qc.invalidateQueries()
              }}
            >
              {PEOPLE.map((p) => (
                <DropdownMenuRadioItem key={p.id} value={p.id}>
                  {p.name}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
            <DropdownMenuSeparator />
            <div className="px-2 py-1 text-[11px] text-text-faint">Mock only. Changes what Today and permissions show.</div>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  )
}
