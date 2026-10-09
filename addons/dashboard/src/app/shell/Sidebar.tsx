import { toast } from 'sonner'
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useRouterState } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  ChevronsUpDown,
  Ellipsis,
  Files,
  LayoutDashboard,
  ListChecks,
  PanelLeftClose,
  PanelLeftOpen,
  Pin,
  PinOff,
  Settings,
  ShieldCheck,
  ShieldOff,
  SquareKanban,
} from 'lucide-react'
import { api } from '@/api/client'
import { activeGrantOf } from '@/api/grants'
import { roleOf } from '@/api/permissions'
import { useAttention } from '../attention'
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
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { iconByName } from '../icons'
import { useWorkspace } from '../workspace'
import { useShellState } from './ShellUi'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import { PIN_ADDON_EVENT } from './pinEvent'

const RailContext = createContext(false)

/** At most this many addons sit in the sidebar; the rest are under "More addons". */
const MAX_PINNED = 6
const PINS_KEY = (person: string, ws: string) => `orch.sidebar.pins.${person}.${ws}`

function readPins(person: string, ws: string): string[] | null {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(PINS_KEY(person, ws)) ?? 'null')
    return Array.isArray(v) && v.every((x) => typeof x === 'string') ? v : null
  } catch {
    return null
  }
}

/**
 * Which addon pages sit in the sidebar: the viewer's choice (kept per workspace in this browser), else the first
 * six by install order. The rest are reached through "More addons".
 */
function usePinnedAddons(person: string | undefined, ws: string | undefined, all: string[]) {
  const [stored, setStored] = useState<string[] | null>(() => (ws && person ? readPins(person, ws) : null))
  useEffect(() => setStored(ws && person ? readPins(person, ws) : null), [person, ws])
  const pinned = (stored ? all.filter((k) => stored.includes(k)) : all).slice(0, MAX_PINNED)
  const toggle = (key: string) => {
    const next = pinned.includes(key) ? pinned.filter((k) => k !== key) : pinned.length < MAX_PINNED ? [...pinned, key] : pinned
    setStored(next)
    try {
      if (ws && person) localStorage.setItem(PINS_KEY(person, ws), JSON.stringify(next))
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }
  // "Pin to sidebar" from the toast after an install: add the page unless it is there or the sidebar is full.
  const latest = useRef({ pinned, toggle })
  latest.current = { pinned, toggle }
  useEffect(() => {
    const on = (e: Event) => {
      const key = (e as CustomEvent<string>).detail
      if (typeof key !== 'string') return
      if (latest.current.pinned.includes(key)) toast.message('Already pinned')
      else if (latest.current.pinned.length >= MAX_PINNED) toast.message('Sidebar is full — unpin one')
      else {
        latest.current.toggle(key)
        toast.success('Pinned to the sidebar')
      }
    }
    window.addEventListener(PIN_ADDON_EVENT, on)
    return () => window.removeEventListener(PIN_ADDON_EVENT, on)
  }, [])
  return { pinned, toggle, full: pinned.length >= MAX_PINNED }
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

/** The addon's icon; in the icon rail a corner badge keeps the orange "A" visible when the label is hidden. */
function AddonNavIcon({ item, collapsed }: { item: { addon: string; icon?: string }; collapsed: boolean }) {
  const Icon = iconByName(item.icon)
  return (
    <span className="relative">
      <Icon className="size-4" data-icon={item.icon} />
      {collapsed && <AddonBadge name={item.addon} className="absolute -right-1.5 -top-1.5 size-2.5 rounded-[3px] text-[7px]" />}
    </span>
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
  const attention = useAttention(workspace?.id)
  // The viewer's own active grant in the current workspace (revoke, re-issue and switching all show).
  const ws = workspace?.id
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const grant = grants.data && today.data ? activeGrantOf(grants.data, me?.person, Date.parse(today.data.now)) : undefined
  const grantTime = grant?.until.slice(11, 16)
  const { railCollapsed: collapsed, toggleRail: toggle } = useShellState()
  const itemKey = (i: { addon: string; id: string }) => `${i.addon}/${i.id}`
  const { pinned, toggle: togglePin, full } = usePinnedAddons(me?.person, ws, navItems.map(itemKey))
  const shown = navItems.filter((i) => pinned.includes(itemKey(i)))
  const [moreOpen, setMoreOpen] = useState(false)
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  // The nav scrolls when the window is short; a fade at the edge says there is more below.
  const navRef = useRef<HTMLElement>(null)
  const [moreBelow, setMoreBelow] = useState(false)
  useEffect(() => {
    const el = navRef.current
    if (!el) return
    const read = () => setMoreBelow(el.scrollHeight - el.scrollTop - el.clientHeight > 4)
    read()
    el.addEventListener('scroll', read)
    window.addEventListener('resize', read)
    return () => {
      el.removeEventListener('scroll', read)
      window.removeEventListener('resize', read)
    }
  }, [navItems.length, shown.length, collapsed])

  const link = cn(
    // Keyboard focus is a brand ring, not the grey fill of the current page (aria-current) or of hover.
    'flex items-center gap-2.5 rounded-md text-[13px] text-text-muted outline-none transition-colors hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand',
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

        <div className="relative flex min-h-0 flex-1 flex-col">
        <nav ref={navRef} className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="Main">
          {CORE_NAV.map(({ to, label: text, icon: Icon }) => (
            <RailTip key={to} label={text}>
              <Link to={to} className={link} activeProps={activeProps} activeOptions={{ exact: to === '/' || to === '/tickets', includeSearch: false }} aria-label={to === '/' && attention.badge > 0 ? `${text}, ${attention.badge} need you` : text}>
                <span className="relative">
                  <Icon className="size-4" />
                  {collapsed && to === '/' && attention.badge > 0 && (
                    <span className="absolute -right-1.5 -top-1.5 size-2 rounded-full bg-brand" aria-hidden />
                  )}
                </span>
                <span className={cn('flex-1', label)}>{text}</span>
                {!collapsed && to === '/' && attention.badge > 0 && (
                  <span className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">{attention.badge}</span>
                )}
              </Link>
            </RailTip>
          ))}

          {navItems.length > 0 && (
            <div className="mt-4 space-y-0.5">
              {collapsed ? (
                <div className="mx-2 mb-1 border-t border-border" />
              ) : (
                <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-text-faint">Addons</div>
              )}
              {shown.map((item) => (
                <RailTip key={itemKey(item)} label={`${item.title}${previews.has(item.addon) ? ' (Preview)' : ''} · from addon ${item.addon}`}>
                  <Link to="/addon/$name/$page" params={{ name: item.addon, page: item.id }} className={link} activeProps={activeProps} aria-label={`${item.title}${previews.has(item.addon) ? ', Preview' : ''}`}>
                    <AddonNavIcon item={item} collapsed={collapsed} />
                    {!collapsed && (
                      <>
                        <span className="flex-1 truncate">{item.title}</span>
                        <PreviewChip name={item.addon} />
                        <AddonBadge name={item.addon} />
                      </>
                    )}
                  </Link>
                </RailTip>
              ))}
              {navItems.length > shown.length && (
                <Popover open={moreOpen} onOpenChange={setMoreOpen}>
                  <RailTip label={`More addons (${navItems.length - shown.length})`}>
                    <PopoverTrigger asChild>
                      <button
                        type="button"
                        aria-label={`More addons (${navItems.length - shown.length})`}
                        className={cn(link, 'w-full outline-none focus-visible:ring-2 focus-visible:ring-brand', navItems.some((i) => !shown.includes(i) && pathname === `/addon/${i.addon}/${i.id}`) && 'bg-surface-2 text-text')}
                      >
                        <Ellipsis className="size-4" />
                        <span className={cn('flex-1 text-left', label)}>More addons ({navItems.length - shown.length})</span>
                      </button>
                    </PopoverTrigger>
                  </RailTip>
                  <PopoverContent side="right" align="end" className="w-72 p-1.5">
                    <p className="px-2 pb-1 pt-0.5 text-[11px] text-text-faint">All addons in this workspace. Pin up to {MAX_PINNED} to the sidebar.</p>
                    <ul>
                      {navItems.map((item) => {
                        const isPinned = pinned.includes(itemKey(item))
                        return (
                          <li key={itemKey(item)} className="flex items-center gap-1">
                            <Link
                              to="/addon/$name/$page"
                              params={{ name: item.addon, page: item.id }}
                              onClick={() => setMoreOpen(false)}
                              className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] text-text-muted hover:bg-surface-2 hover:text-text"
                              aria-label={`${item.title}${previews.has(item.addon) ? ', Preview' : ''}`}
                            >
                              <AddonNavIcon item={item} collapsed={false} />
                              <span className="flex-1 truncate">{item.title}</span>
                              <PreviewChip name={item.addon} />
                              <AddonBadge name={item.addon} />
                            </Link>
                            <button
                              type="button"
                              aria-label={`${isPinned ? 'Unpin' : 'Pin'} ${item.title} ${isPinned ? 'from' : 'to'} the sidebar`}
                              aria-pressed={isPinned}
                              disabled={!isPinned && full}
                              title={!isPinned && full ? `The sidebar holds ${MAX_PINNED}. Unpin one first.` : undefined}
                              onClick={() => togglePin(itemKey(item))}
                              className="rounded-md p-1.5 text-text-faint outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-brand disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              {isPinned ? <PinOff className="size-3.5" /> : <Pin className="size-3.5" />}
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                  </PopoverContent>
                </Popover>
              )}
            </div>
          )}
        </nav>
        {moreBelow && <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-sidebar to-transparent" />}
        </div>

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
                    {p.name} · {roleOf(workspace, p.id) ?? 'not a member'}
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
  { to: '/artifacts', label: 'Artifacts', icon: Files },
  { to: '/agents', label: 'Agents', icon: Bot },
] as const
