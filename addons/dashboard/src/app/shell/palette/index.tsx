import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { Bot, Check, Clock, FileText, LayoutDashboard, ListChecks, MessageSquare, MessageSquareReply, Plus, Save, Settings, SquareKanban, User, Zap, ArrowRightLeft, Building2 } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, STATUSES, type ActionRequest } from '@/api/types'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { CommandDialog, CommandEmpty, CommandInput, CommandList } from '@/components/ui/command'
import { iconByName } from '../../icons'
import { useWorkspace } from '../../workspace'
import { STATUS_LABEL } from '../../pages/board/lib'
import { availableActions, GATE_LABEL } from '../../pages/ticket/actions'
import { SignDialog } from '../../pages/ticket/SignDialog'
import { useViewer, type HumanAction } from '../../pages/ticket/shared'
import { requestSaveView } from '../../pages/tickets/saveViewRequest'
import { useShellState } from '../ShellUi'
import { keysFor } from '../shortcuts'
import { Group, matches, type Entry } from './groups'
import { describePath, loadRecent, recordRecent, type RecentItem } from './recent'

type Mode = null | 'comment' | 'move' | 'ask-to' | { ask: string }

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

const PLACEHOLDER = 'Search tickets or run a command...'

/** Global command palette: ⌘K / Ctrl+K. `>` commands, `#` tickets, `@` people. */
export function CommandPalette() {
  const { paletteOpen, setPaletteOpen } = useShellState()
  const { workspace, workspaces, setWorkspaceId } = useWorkspace()
  const router = useRouter()
  const qc = useQueryClient()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const ticketKey = /^\/ticket\/([^/]+)$/.exec(pathname)?.[1]
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<Mode>(null)
  const [signing, setSigning] = useState<HumanAction | null>(null)
  const { data: addons = [] } = useAddons()
  const addonNav = useSlot('nav')
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const person = me?.person
  const role = workspace?.members.find((m) => m.person === person)?.role
  const canEdit = !!role && role !== 'viewer'

  // Recent items: every visited ticket and page, per viewer.
  const [recent, setRecent] = useState<RecentItem[]>([])
  useEffect(() => {
    if (!person) return
    const item = describePath(pathname)
    setRecent(item ? recordRecent(person, item) : loadRecent(person))
  }, [pathname, person])

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
    if (!paletteOpen) {
      setQ('')
      setMode(null)
    }
  }, [paletteOpen])

  // The prefix restricts what is listed; the rest is the search text.
  const prefix = mode ? '' : q.startsWith('>') || q.startsWith('#') || q.startsWith('@') ? q[0] : ''
  const text = mode ? q : q.slice(prefix.length)
  const needle = text.trim().toLowerCase()
  const dq = useDebounced(text.trim(), 150)

  const { data: tickets = [] } = useQuery({
    queryKey: ['palette-tickets', workspace?.id, dq],
    queryFn: () => api.listTickets(workspace!.id, { q: dq }),
    enabled: paletteOpen && !!workspace && !mode && (prefix === '' || prefix === '#') && dq.length > 0,
    placeholderData: (prev) => prev,
  })

  const ticket = useQuery({
    queryKey: ['ticket', ticketKey],
    queryFn: () => api.getTicket(ticketKey!),
    enabled: paletteOpen && !!ticketKey,
    retry: false,
  })
  const viewer = useViewer(ticketKey ?? '')

  const close = () => setPaletteOpen(false)
  const go = (to: string) => {
    close()
    void router.navigate({ to } as never)
  }
  const fail = (e: unknown) => toast.error(e instanceof ApiError ? e.message : e instanceof Error ? e.message : 'Request failed')

  const post = async (req: ActionRequest, done: string) => {
    close()
    try {
      await api.postAction(ticketKey!, req)
      await qc.invalidateQueries()
      toast.success(done)
    } catch (e) {
      fail(e)
    }
  }

  // ------------------------------------------------------------ entries
  const goTo: Entry[] = [
    { id: 'today', label: 'Go to Today', icon: <LayoutDashboard />, keys: keysFor('go.today'), run: () => go('/') },
    { id: 'board', label: 'Go to Board', icon: <SquareKanban />, keys: keysFor('go.board'), run: () => go('/board') },
    { id: 'tickets', label: 'Go to Tickets', icon: <ListChecks />, keys: keysFor('go.tickets'), run: () => go('/tickets') },
    { id: 'agents', label: 'Go to Agents', icon: <Bot />, keys: keysFor('go.agents'), run: () => go('/agents') },
    { id: 'settings', label: 'Go to Settings', icon: <Settings />, run: () => go('/settings') },
    ...addonNav.map((n): Entry => {
      const Icon = iconByName(n.icon)
      return { id: `${n.addon}/${n.id}`, label: `Go to ${n.title}`, icon: <Icon />, hint: n.addon, addon: n.addon, run: () => go(`/addon/${n.addon}/${n.id}`) }
    }),
  ]

  const create: Entry[] = [
    ...(canEdit || !role ? [{ id: 'new-ticket', label: 'New ticket', icon: <Plus />, hint: 'create', keys: keysFor('new-ticket'), run: () => go('/tickets/new') }] : []),
    {
      id: 'save-view',
      label: 'Save view…',
      icon: <Save />,
      run: () => {
        go('/tickets')
        requestSaveView()
      },
    },
  ]

  const addonCommands: Entry[] = addons
    .filter((a) => a.enabled && workspace?.addons[a.name]?.enabled)
    .flatMap((a) => (a.commands ?? []).map((c) => ({ addon: a.name, ...c })))
    .map((c) => ({
      id: `${c.addon}/${c.id}`,
      label: c.title,
      icon: <Zap />,
      hint: c.addon,
      addon: c.addon,
      run: async () => {
        close()
        try {
          const res = await api.runAddonAction(c.addon, c.action, { ws: workspace!.id, ...(ticketKey ? { ticket: ticketKey } : {}) })
          toast.success(res.message)
          if (res.changed) void qc.invalidateQueries()
        } catch (e) {
          fail(e)
        }
      },
    }))

  const onTicket = useMemo<Entry[]>(() => {
    const t = ticket.data
    if (!ticketKey || !t || !viewer.ready || !canEdit) return []
    const av = availableActions(t, viewer)
    const sign = (a: HumanAction) => {
      close()
      setSigning(a)
    }
    const out: Entry[] = []
    // Claim and Release are for agents only (see the claim box on the ticket page): never offered to people.
    out.push({ id: 'comment', label: 'Comment', icon: <MessageSquare />, run: () => (setQ(''), setMode('comment')) })
    out.push({ id: 'ask', label: 'Ask a question', icon: <MessageSquareReply />, run: () => (setQ(''), setMode('ask-to')) })
    if (role === 'owner' || role === 'maintainer') out.push({ id: 'move', label: 'Move to…', icon: <ArrowRightLeft />, run: () => (setQ(''), setMode('move')) })
    for (const g of av.approve) out.push({ id: `approve-${g}`, label: `Approve ${GATE_LABEL[g].toLowerCase()}`, icon: <Check />, run: () => sign({ kind: 'approve', gate: g }) })
    if (av.verdict) out.push({ id: 'verdict', label: 'Give verdict', icon: <Check />, run: () => sign({ kind: 'verdict' }) })
    return out
  }, [ticket.data, ticketKey, viewer.ready, viewer.person, viewer.role, canEdit, role])

  const switchWs: Entry[] = workspaces
    .filter((w) => w.id !== workspace?.id)
    .map((w) => ({
      id: w.id,
      label: `Switch to ${w.prefix}`,
      icon: <Building2 />,
      hint: w.name,
      run: () => {
        close()
        setWorkspaceId(w.id)
      },
    }))

  const people: Entry[] = (workspace?.members ?? []).map((m) => ({
    id: m.person,
    label: m.name,
    icon: <User />,
    hint: m.role,
    run: () => {
      close()
      void router.navigate({ to: '/tickets', search: { person: m.person } })
    },
  }))

  const visible = (entries: Entry[]) => entries.filter((e) => matches(needle, typeof e.label === 'string' ? e.label : '', e.hint))
  const ticketEntries: Entry[] = tickets.slice(0, 8).map((t) => ({
    id: t.key,
    label: (
      <>
        <span className="w-24 shrink-0 font-mono text-[12px] text-text-faint">{t.key}</span>
        <span className="flex-1 truncate">{t.title}</span>
        <span className="text-[11px] text-text-faint">{STATUS_LABEL[t.status]}</span>
      </>
    ),
    run: () => go(`/ticket/${t.key}`),
  }))
  const recentEntries: Entry[] = recent.map((r) => ({
    id: r.path,
    label: r.label,
    icon: r.kind === 'ticket' ? <FileText /> : <Clock />,
    run: () => go(r.path),
  }))

  // ------------------------------------------------------------ sub-prompts (comment, ask, move)
  const members = (workspace?.members ?? []).filter((m) => m.person !== person)
  let body
  if (mode === 'comment' || (typeof mode === 'object' && mode)) {
    const ask = typeof mode === 'object' && mode ? mode.ask : null
    body = (
      <Group
        heading={ask ? `Ask ${members.find((m) => m.person === ask)?.name ?? ask}` : 'Comment'}
        entries={[
          {
            id: 'post',
            label: ask ? 'Send question' : 'Post comment',
            icon: <MessageSquare />,
            disabled: !text.trim(),
            run: () => void post(ask ? { action: 'ask', to: ask, text: text.trim() } : { action: 'comment', text: text.trim() }, ask ? 'Question sent' : 'Comment posted'),
          },
        ]}
      />
    )
  } else if (mode === 'ask-to') {
    body = <Group heading="Ask whom" entries={visible(members.map((m) => ({ id: m.person, label: m.name, icon: <User />, hint: m.role, run: () => (setQ(''), setMode({ ask: m.person })) })))} />
  } else if (mode === 'move') {
    const cur = ticket.data?.status
    body = (
      <Group
        heading="Move to"
        entries={visible(
          STATUSES.filter((s) => s !== cur && s !== 'done').map((s) => ({
            id: s,
            label: STATUS_LABEL[s],
            run: () => void post({ action: 'set_status', status: s }, `Moved to ${STATUS_LABEL[s]}`),
          })),
        )}
      />
    )
  } else {
    const commandsOnly = prefix === '>'
    body = (
      <>
        {!prefix && <Group heading="Recent" entries={visible(recentEntries)} />}
        {(!prefix || commandsOnly) && <Group heading="On this ticket" entries={visible(onTicket)} />}
        {(prefix === '' || prefix === '#') && <Group heading="Tickets" entries={ticketEntries} />}
        {(!prefix || commandsOnly) && <Group heading="Go to" entries={visible(goTo)} />}
        {(!prefix || commandsOnly) && <Group heading="Create" entries={visible(create)} />}
        {(!prefix || commandsOnly) && <Group heading="Addon commands" entries={visible(addonCommands)} />}
        {prefix === '@' && <Group heading="People" entries={visible(people)} />}
        {(!prefix || commandsOnly) && <Group heading="Workspace" entries={visible(switchWs)} />}
      </>
    )
  }

  const placeholder = mode === 'comment' ? 'Write a comment, then Enter...' : typeof mode === 'object' && mode ? 'Write the question, then Enter...' : mode === 'ask-to' ? 'Ask whom?' : mode === 'move' ? 'Move to which status?' : PLACEHOLDER

  return (
    <>
      <CommandDialog open={paletteOpen} onOpenChange={setPaletteOpen} shouldFilter={false} title="Command palette" description="Search tickets or run a command. Type > for commands, # for tickets, @ for people." className="sm:max-w-xl">
        <CommandInput
          value={q}
          onValueChange={setQ}
          placeholder={placeholder}
          onKeyDown={(e) => {
            if (e.key === 'Backspace' && q === '' && mode) {
              e.preventDefault()
              setMode(null)
            }
          }}
        />
        <CommandList className="max-h-[360px]">
          <CommandEmpty>No results.</CommandEmpty>
          {body}
        </CommandList>
      </CommandDialog>
      {ticket.data && <SignDialog ticket={ticket.data} action={signing} onClose={() => setSigning(null)} />}
    </>
  )
}
