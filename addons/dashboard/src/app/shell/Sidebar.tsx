import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { Link } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  ChevronsUpDown,
  LayoutDashboard,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  ShieldCheck,
  ShieldOff,
  SquareKanban,
} from 'lucide-react'
import { api } from '@/api/client'
import { activeGrantOf } from '@/api/grants'
import { useRole } from '../useRole'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { PreviewChip } from '@/addon-ui/PreviewChip'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { OrbitMark } from '@/brand/OrbitMark'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
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
import { WorkspaceSwitcher } from './WorkspaceSwitcher'

type Pref = 'auto' | 'wide' | 'narrow'
const PREF_KEY = 'orch.sidebar'
const RailContext = createContext(false)

function readPref(): Pref {
  try {
    const v = localStorage.getItem(PREF_KEY)
    return v === 'wide' || v === 'narrow' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

/** Wide or narrow (icon rail). The user's choice wins; without one, the rail is used below 1280 px. */
export function useSidebarCollapsed() {
  const [pref, setPref] = useState<Pref>(readPref)
  const [narrowWindow, setNarrowWindow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1280)
  useEffect(() => {
    const onResize = () => setNarrowWindow(window.innerWidth < 1280)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const collapsed = pref === 'auto' ? narrowWindow : pref === 'narrow'
  const toggle = useCallback(() => {
    const next: Pref = collapsed ? 'wide' : 'narrow'
    setPref(next)
    try {
      localStorage.setItem(PREF_KEY, next)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }, [collapsed])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '[' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
      e.preventDefault()
      toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggle])
  return { collapsed, toggle }
}

/** In the icon rail the label is hidden and a tooltip carries it. */
function RailTip({ label, children }: { label: string; children: ReactNode }) {
  const collapsed = useContext(RailContext)
  if (!collapsed) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  )
}

function ToggleButton({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar'
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onToggle}
          aria-label={label}
          aria-expanded={!collapsed}
          className="rounded-md p-1.5 text-text-faint outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
        >
          <Icon className="size-4" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="right">
        {label} <kbd className="ml-1 rounded bg-surface-3 px-1 font-mono text-[10px]">[</kbd>
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
  const { workspace } = useWorkspace()
  const qc = useQueryClient()
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const navItems = useSlot('nav')
  const { data: packages } = useAddons()
  const previews = new Set(packages?.filter((a) => a.preview).map((a) => a.name))
  const role = useRole() ?? me?.role
  // The viewer's own active grant in the current workspace (revoke, re-issue and switching all show).
  const ws = workspace?.id
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const grant = grants.data && today.data ? activeGrantOf(grants.data, me?.person, Date.parse(today.data.now)) : undefined
  const grantTime = grant?.until.slice(11, 16)
  const { collapsed, toggle } = useSidebarCollapsed()

  const link = cn(
    'flex items-center gap-2.5 rounded-md text-[13px] text-text-muted transition-colors hover:bg-surface-2 hover:text-text',
    collapsed ? 'justify-center px-0 py-2' : 'px-2.5 py-1.5',
  )
  const activeProps = { className: 'bg-surface-2 !text-text' }
  const label = collapsed ? 'hidden' : ''

  return (
    <RailContext.Provider value={collapsed}>
      <aside
        data-collapsed={collapsed}
        className={cn('flex shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-150', collapsed ? 'w-14' : 'w-[232px]')}
      >
        <div className={cn('flex h-12 items-center gap-2', collapsed ? 'justify-center' : 'px-3.5')}>
          <OrbitMark size={24} />
          {!collapsed && (
            <>
              <span className="flex-1 font-bold tracking-[-0.02em] text-text" style={{ fontSize: 18 }}>
                orch
              </span>
              <ToggleButton collapsed={collapsed} onToggle={toggle} />
            </>
          )}
        </div>
        {collapsed && (
          <div className="flex justify-center pb-1">
            <ToggleButton collapsed={collapsed} onToggle={toggle} />
          </div>
        )}

        <div className="px-2 pb-1">
          <WorkspaceSwitcher collapsed={collapsed} viewer={me?.person} />
        </div>

        <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="Main">
          {CORE_NAV.map(({ to, label: text, icon: Icon }) => (
            <RailTip key={to} label={text}>
              <Link to={to} className={link} activeProps={activeProps} activeOptions={{ exact: to === '/' }} aria-label={text}>
                <span className="relative">
                  <Icon className="size-4" />
                  {collapsed && to === '/' && workspace && workspace.needs_you > 0 && (
                    <span className="absolute -right-1.5 -top-1.5 size-2 rounded-full bg-brand" aria-hidden />
                  )}
                </span>
                <span className={cn('flex-1', label)}>{text}</span>
                {!collapsed && to === '/' && workspace && workspace.needs_you > 0 && (
                  <span className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">{workspace.needs_you}</span>
                )}
              </Link>
            </RailTip>
          ))}

          {navItems.length > 0 && (
            <div className="mt-4">
              {collapsed ? (
                <div className="mx-2 mb-1 border-t border-border" />
              ) : (
                <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-text-faint">Addons</div>
              )}
              {navItems.map((item) => {
                const Icon = iconByName(item.icon)
                return (
                  <RailTip key={`${item.addon}/${item.id}`} label={`${item.title}${previews.has(item.addon) ? ' (Preview)' : ''} · from addon ${item.addon}`}>
                    <Link
                      to="/addon/$name/$page"
                      params={{ name: item.addon, page: item.id }}
                      className={link}
                      activeProps={activeProps}
                      aria-label={item.title}
                    >
                      <span className="relative">
                        <Icon className="size-4" />
                        {/* Icon rail: the corner badge keeps the orange "A" visible when the label is hidden. */}
                        {collapsed && (
                          <AddonBadge name={item.addon} className="absolute -right-1.5 -top-1.5 size-2.5 rounded-[3px] text-[7px]" />
                        )}
                      </span>
                      {!collapsed && (
                        <>
                          <span className="flex-1 truncate">{item.title}</span>
                          <PreviewChip name={item.addon} />
                          <AddonBadge name={item.addon} />
                        </>
                      )}
                    </Link>
                  </RailTip>
                )
              })}
            </div>
          )}
        </nav>

        <div className="space-y-1 border-t border-border p-2">
          <RailTip label="Settings">
            <Link to="/settings" className={link} activeProps={activeProps} aria-label="Settings">
              <Settings className="size-4" />
              <span className={label}>Settings</span>
            </Link>
          </RailTip>

          <RailTip label={grant ? `Agents granted until ${grantTime}` : 'No grant · run orch grant'}>
            <div
              className={cn(
                'flex items-center gap-2 py-1 text-[11px]',
                collapsed ? 'justify-center px-0' : 'px-2.5',
                grant ? 'text-brand' : 'text-warning',
              )}
            >
              {grant ? <ShieldCheck className="size-3.5 shrink-0" /> : <ShieldOff className="size-3.5 shrink-0" />}
              <span className={label}>{grant ? `agents granted until ${grantTime}` : 'no grant · run orch grant'}</span>
            </div>
          </RailTip>

          <DropdownMenu>
            <RailTip label={me ? `${me.name} (${role})` : 'Viewer'}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  aria-label="Viewing as"
                  className={cn('flex w-full items-center gap-2 rounded-md py-1.5 text-left hover:bg-surface-2', collapsed ? 'justify-center px-0' : 'px-2')}
                >
                  <Avatar className="size-7">
                    <AvatarFallback className="bg-surface-3 text-[11px]">{me?.name.slice(0, 2).toUpperCase()}</AvatarFallback>
                  </Avatar>
                  <span className={cn('min-w-0 flex-1', label)}>
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
    </RailContext.Provider>
  )
}

const CORE_NAV = [
  { to: '/', label: 'Today', icon: LayoutDashboard },
  { to: '/board', label: 'Board', icon: SquareKanban },
  { to: '/tickets', label: 'Tickets', icon: ListChecks },
  { to: '/agents', label: 'Agents', icon: Bot },
] as const
