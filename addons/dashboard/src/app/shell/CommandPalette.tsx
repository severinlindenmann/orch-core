import { useEffect, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, LayoutDashboard, ListChecks, Plus, Settings, SquareKanban, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from '@/components/ui/command'
import { iconByName } from '../icons'
import { useWorkspace } from '../workspace'
import { useShellState } from './ShellUi'

const NAV = [
  { label: 'Go to Today', to: '/', icon: LayoutDashboard, keys: 'G T' },
  { label: 'Go to Board', to: '/board', icon: SquareKanban, keys: 'G B' },
  { label: 'Go to Tickets', to: '/tickets', icon: ListChecks, keys: 'G K' },
  { label: 'Go to Agents', to: '/agents', icon: Bot, keys: 'G A' },
  { label: 'Go to Settings', to: '/settings', icon: Settings, keys: 'G S' },
] as const

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Global command palette: ⌘K / Ctrl+K. Tickets (server search), navigation, "New ticket", addon commands. */
export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, setNewTicketOpen } = useShellState()
  const { workspace } = useWorkspace()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [q, setQ] = useState('')
  const dq = useDebounced(q.trim(), 150)
  const { data: addons = [] } = useAddons()
  const addonNav = useSlot('nav')

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(!paletteOpen)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [paletteOpen, setPaletteOpen])

  useEffect(() => {
    if (!paletteOpen) setQ('')
  }, [paletteOpen])

  const { data: tickets = [] } = useQuery({
    queryKey: ['palette-tickets', workspace?.id, dq],
    queryFn: () => api.listTickets(workspace!.id, { q: dq }),
    enabled: paletteOpen && !!workspace && dq.length > 0,
  })

  const needle = q.trim().toLowerCase()
  const match = (s: string) => !needle || s.toLowerCase().includes(needle)
  const close = () => setPaletteOpen(false)

  const addonCommands = addons
    .filter((a) => a.enabled && workspace?.addons[a.name]?.enabled)
    .flatMap((a) => (a.commands ?? []).map((c) => ({ addon: a.name, ...c })))
    .filter((c) => match(`${c.title} ${c.addon}`))

  return (
    <CommandDialog open={paletteOpen} onOpenChange={setPaletteOpen} shouldFilter={false} title="Command palette" description="Search tickets or run a command" className="sm:max-w-xl">
      <CommandInput value={q} onValueChange={setQ} placeholder="Search tickets or run a command..." />
      <CommandList className="max-h-[360px]">
        <CommandEmpty>No results.</CommandEmpty>
        {tickets.length > 0 && (
          <CommandGroup heading="Tickets">
            {tickets.slice(0, 8).map((t) => (
              <CommandItem
                key={t.key}
                value={`ticket-${t.key}`}
                onSelect={() => {
                  close()
                  void navigate({ to: '/ticket/$key', params: { key: t.key } })
                }}
              >
                <span className="w-24 shrink-0 font-mono text-[12px] text-text-faint">{t.key}</span>
                <span className="flex-1 truncate">{t.title}</span>
                <span className="text-[11px] text-text-faint">{t.status}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        <CommandGroup heading="Actions">
          {match('new ticket create') && (
            <CommandItem
              value="new-ticket"
              onSelect={() => {
                close()
                setNewTicketOpen(true)
              }}
            >
              <Plus />
              New ticket
            </CommandItem>
          )}
          {NAV.filter((n) => match(n.label)).map((n) => (
            <CommandItem
              key={n.to}
              value={n.to}
              onSelect={() => {
                close()
                void navigate({ to: n.to })
              }}
            >
              <n.icon />
              {n.label}
              <CommandShortcut>{n.keys}</CommandShortcut>
            </CommandItem>
          ))}
        </CommandGroup>
        {(addonNav.some((n) => match(n.title)) || addonCommands.length > 0) && (
          <CommandGroup heading="Addons">
            {addonNav
              .filter((n) => match(`go to ${n.title} ${n.addon}`))
              .map((n) => {
                const Icon = iconByName(n.icon)
                return (
                  <CommandItem
                    key={`${n.addon}/${n.id}`}
                    value={`addon-${n.addon}-${n.id}`}
                    onSelect={() => {
                      close()
                      void navigate({ to: '/addon/$name/$page', params: { name: n.addon, page: n.id } })
                    }}
                  >
                    <Icon />
                    Go to {n.title}
                    <AddonBadge name={n.addon} className="ml-auto" />
                  </CommandItem>
                )
              })}
            {addonCommands.map((c) => (
              <CommandItem
                key={`${c.addon}/${c.id}`}
                value={`cmd-${c.addon}-${c.id}`}
                onSelect={async () => {
                  close()
                  try {
                    const res = await api.runAddonAction(c.addon, c.action)
                    toast.success(res.message)
                    if (res.changed) void qc.invalidateQueries()
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : 'Command failed')
                  }
                }}
              >
                <Zap />
                {c.title}
                <AddonBadge name={c.addon} className="ml-auto" />
              </CommandItem>
            ))}
          </CommandGroup>
        )}
      </CommandList>
    </CommandDialog>
  )
}
