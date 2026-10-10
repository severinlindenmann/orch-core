import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { Bot, Check, Link2, Clock, FileText, Files, LayoutDashboard, ListChecks, MessageSquare, MessageSquareReply, Plus, Save, Settings, SquareKanban, User, Zap, ArrowRightLeft, Building2 } from 'lucide-react'
import { toast } from 'sonner'
import { addonActive } from '@/api/addons'
import { can } from '@/api/permissions'
import { api } from '@/api/client'
import { STATUSES, type ActionRequest, type Status } from '@/api/types'
import { useAddons, useSlot } from '@/addon-ui/slots'
import { useRunAddonAction } from '@/addon-ui/useRunAddonAction'
import { CommandDialog, CommandEmpty, CommandInput, CommandList } from '@/components/ui/command'
import { iconByName } from '../../icons'
import { useWorkspace } from '../../workspace'
import { useRole } from '../../useRole'
import { STATUS_LABEL } from '../../pages/board/lib'
import { availableActions, GATE_LABEL } from '../../pages/ticket/actions'
import { SignDialog } from '../../pages/ticket/SignDialog'
import { statusLabel, useViewer, type HumanAction } from '../../pages/ticket/shared'
import { requestSaveView } from '../../pages/tickets/saveViewRequest'
import { useShellActions, useShellState } from '../ShellUi'
import { guessType, quickProblem, quickTitle } from '../../pages/new-ticket/quickRules'
import { useQuickCreate } from '../../pages/new-ticket/useQuickCreate'
import { keysFor } from '../shortcuts'
import { Group, matches, ticketRank, type Entry } from './groups'
import { describePath, loadRecent, recordRecent, type RecentItem } from './recent'
import { toastApiError } from '@/app/toast'
import { useCopyLink } from '../../copyLink'

type Mode = null | 'comment' | 'move' | 'move-pick' | 'ask-to' | 'quick' | { ask: string }

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
  const { paletteOpen, setPaletteOpen, paletteSeed, setPaletteSeed } = useShellState()
  const { workspace, workspaces, switchWorkspace } = useWorkspace()
  const router = useRouter()
  const qc = useQueryClient()
  const pathname = useRouterState({ select: (s) => s.location.pathname })
  const ticketKey = /^\/ticket\/([^/]+)$/.exec(pathname)?.[1]
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<Mode>(null)
  const [signing, setSigning] = useState<HumanAction | null>(null)
  // "Move ticket to…" on the board: the ticket chosen (or the focused card when ⌘K was pressed on one).
  const [moveTarget, setMoveTarget] = useState<{ key: string; status?: Status } | null>(null)
  const focusedCard = useRef<{ key: string; status: Status } | null>(null)
  const { data: addons = [] } = useAddons()
  const addonNav = useSlot('nav')
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const person = me?.person
  const role = useRole()
  const runAddon = useRunAddonAction(ticketKey)
  const { openNewTicket } = useShellActions()
  const quick = useQuickCreate(workspace?.id)

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
        const d = (document.activeElement as HTMLElement | null)?.dataset
        focusedCard.current = d?.ticket && d.status ? { key: d.ticket, status: d.status as Status } : null
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
      setMoveTarget(null)
    } else if (paletteSeed) {
      // Opened by a shortcut that wants to show something (a viewer pressing c): start with that search.
      setQ(paletteSeed)
      setPaletteSeed('')
    }
  }, [paletteOpen, paletteSeed, setPaletteSeed])

  // A sub-prompt (Quick ticket, Comment, ...) takes typing at once, also when it was picked with the mouse.
  useEffect(() => {
    if (mode) document.querySelector<HTMLInputElement>('[cmdk-input]')?.focus()
  }, [mode])

  // The prefix restricts what is listed; the rest is the search text.
  const prefix = mode ? '' : q.startsWith('>') || q.startsWith('#') || q.startsWith('@') ? q[0] : ''
  const text = mode ? q : q.slice(prefix.length)
  // A query of only dashes or underscores is no query.
  const needle = /^[\s_-]*$/.test(text) ? '' : text.trim().toLowerCase()
  const debounced = useDebounced(needle ? text.trim() : '', 150)
  // A key (DEMO-0041) is searched at once, so Enter never opens the previous search's first hit.
  const dq = needle && /^[a-z]{2,}-?\d+$/i.test(text.trim()) ? text.trim() : debounced

  const { data: tickets = [] } = useQuery({
    queryKey: ['palette-tickets', workspace?.id, dq],
    queryFn: () => api.listTickets(workspace!.id, { q: dq }),
    enabled: paletteOpen && !!workspace && (!mode || mode === 'move-pick') && (prefix === '' || prefix === '#') && dq.length > 0,
    placeholderData: (prev) => prev,
  })

  const ticket = useQuery({
    queryKey: ['ticket', ticketKey],
    queryFn: () => api.getTicket(ticketKey!),
    enabled: paletteOpen && !!ticketKey,
    retry: false,
  })
  const viewer = useViewer(ticketKey ?? '')

  const copyLink = useCopyLink()
  const close = () => setPaletteOpen(false)
  const go = (to: string) => {
    close()
    void router.navigate({ to } as never)
  }
  const fail = (e: unknown) => toastApiError(e, 'Request failed')

  const post = async (req: ActionRequest, done: string, key = ticketKey!) => {
    close()
    try {
      await api.postAction(key, req)
      await qc.invalidateQueries()
      toast.success(done)
    } catch (e) {
      fail(e)
    }
  }

  // ------------------------------------------------------------ entries
  const goTo: Entry[] = [
    { id: 'copy-link', label: 'Copy link to this page', icon: <Link2 />, hint: 'link', run: () => (close(), void copyLink()) },
    { id: 'today', label: 'Go to Today', icon: <LayoutDashboard />, keys: keysFor('go.today'), run: () => go('/') },
    { id: 'board', label: 'Go to Board', icon: <SquareKanban />, keys: keysFor('go.board'), run: () => go('/board') },
    { id: 'tickets', label: 'Go to Tickets', icon: <ListChecks />, keys: keysFor('go.tickets'), run: () => go('/tickets') },
    { id: 'artifacts', label: 'Go to Artifacts', icon: <Files />, run: () => go('/artifacts') },
    { id: 'agents', label: 'Go to Agents', icon: <Bot />, keys: keysFor('go.agents'), run: () => go('/agents') },
    { id: 'settings', label: 'Go to Settings', icon: <Settings />, run: () => go('/settings') },
    ...addonNav.map((n): Entry => {
      const Icon = iconByName(n.icon)
      return { id: `${n.addon}/${n.id}`, label: `Go to ${n.title}`, icon: <Icon />, hint: n.addon, addon: n.addon, run: () => go(`/addon/${n.addon}/${n.id}`) }
    }),
  ]

  const create: Entry[] = [
    can(role, 'ticket.create') || !role
      ? {
          id: 'new-ticket',
          label: 'New ticket',
          icon: <Plus />,
          hint: 'create',
          keys: keysFor('new-ticket'),
          run: () => {
            close()
            openNewTicket()
          },
        }
      : {
          id: 'new-ticket',
          label: (
            <>
              New ticket
              <span className="ml-2 text-[12px] text-text-faint">Viewers cannot create tickets</span>
            </>
          ),
          icon: <Plus />,
          hint: 'new ticket create',
          disabled: true,
          run: () => undefined,
        },
    can(role, 'ticket.create') || !role
      ? { id: 'quick-ticket', label: 'Quick ticket…', icon: <Zap />, hint: 'create new ticket one line', run: () => (setQ(''), setMode('quick')) }
      : {
          id: 'quick-ticket',
          label: (
            <>
              Quick ticket…
              <span className="ml-2 text-[12px] text-text-faint">Viewers cannot create tickets</span>
            </>
          ),
          icon: <Zap />,
          hint: 'quick ticket new create',
          disabled: true,
          run: () => undefined,
        },
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

  // Addon commands go through the one action hook: the installed manifest decides who may run each (a viewer gets the
  // viewer-level ones), and signed or agent-starting actions open core's dialog first.
  const addonCommands: Entry[] = addons
    .filter((a) => addonActive(workspace, a.name))
    .flatMap((a) => (a.commands ?? []).map((c) => ({ addon: a.name, ...c })))
    .filter((c) => runAddon.allowed(c.addon, c.action))
    .map((c) => ({
      id: `${c.addon}/${c.id}`,
      label: c.title,
      icon: <Zap />,
      hint: c.addon,
      addon: c.addon,
      run: () => {
        close()
        runAddon.run(c.addon, c.action)
        // A navigation command is quiet (no toast): take the person to the addon's page, where its effect shows.
        const page = addonNav.find((n) => n.addon === c.addon)
        if (runAddon.meta(c.addon, c.action)?.kind === 'navigation' && page) go(`/addon/${page.addon}/${page.id}`)
      },
    }))

  const onBoard: Entry[] =
    pathname === '/board' && can(role, 'ticket.move')
      ? [
          {
            id: 'board-move',
            label: focusedCard.current ? `Move ${focusedCard.current.key} to…` : 'Move ticket to…',
            icon: <ArrowRightLeft />,
            run: () => {
              setQ('')
              if (focusedCard.current) {
                setMoveTarget(focusedCard.current)
                setMode('move')
              } else setMode('move-pick')
            },
          },
        ]
      : []

  const onTicket = useMemo<Entry[]>(() => {
    const t = ticket.data
    // Gate on the viewer's role in the ticket's own workspace, which can differ from the current one.
    const ticketRole = viewer.role
    if (!ticketKey || !t || !viewer.ready || !can(ticketRole, 'ticket.act')) return []
    const av = availableActions(t, viewer)
    const sign = (a: HumanAction) => {
      close()
      setSigning(a)
    }
    const out: Entry[] = []
    // Claim and Release are for agents only (see the claim box on the ticket page): never offered to people.
    out.push({ id: 'comment', label: 'Comment', icon: <MessageSquare />, run: () => (setQ(''), setMode('comment')) })
    out.push({ id: 'ask', label: 'Ask a question', icon: <MessageSquareReply />, run: () => (setQ(''), setMode('ask-to')) })
    if (can(ticketRole, 'ticket.move')) out.push({ id: 'move', label: 'Move to…', icon: <ArrowRightLeft />, run: () => (setQ(''), setMode('move')) })
    for (const g of av.approve) out.push({ id: `approve-${g}`, label: `Approve ${GATE_LABEL[g].toLowerCase()}`, icon: <Check />, run: () => sign({ kind: 'approve', gate: g }) })
    if (av.verdict) out.push({ id: 'verdict', label: 'Give verdict', icon: <Check />, run: () => sign({ kind: 'verdict' }) })
    return out
  }, [ticket.data, ticketKey, viewer.ready, viewer.person, viewer.role])

  const switchWs: Entry[] = workspaces
    .map((w, i) => ({ w, i }))
    .filter(({ w }) => w.id !== workspace?.id)
    .map(({ w, i }) => ({
      id: w.id,
      label: `Switch to ${w.prefix}`,
      icon: <Building2 />,
      hint: w.name,
      keys: keysFor(`workspace.${i + 1}`),
      run: () => {
        close()
        switchWorkspace(w.id)
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
  // The exact key, then key prefixes, then title-word matches come first (stable within a rank), so Enter opens what was typed.
  const ranked = (needle ? tickets : []).map((t, i) => ({ t, i, r: ticketRank(t, needle) })).sort((a, b) => a.r - b.r || a.i - b.i)
  const topRank = ranked[0]?.r ?? 3
  const ticketEntries: Entry[] = ranked.slice(0, 8).map(({ t }) => ({
    id: t.key,
    label: (
      <>
        <span className="w-24 shrink-0 font-mono text-[12px] text-text-faint">{t.key}</span>
        <span className="flex-1 truncate">{t.title}</span>
        <span className="text-[11px] text-text-faint">{statusLabel(t.status, t.landing)}</span>
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
  } else if (mode === 'quick') {
    const problem = quickProblem(text)
    body = (
      <Group
        heading="Quick ticket"
        entries={[
          {
            id: 'quick-create',
            label: problem ? (
              <span className="text-text-muted">Describe the ticket in one line, then Enter</span>
            ) : (
              <span className="min-w-0 flex-1 truncate">
                Create {guessType(text)} in Backlog: “{quickTitle(text)}”
              </span>
            ),
            icon: <Zap />,
            disabled: !!problem || quick.pending,
            run: () => {
              close()
              void quick.create(text)
            },
          },
        ]}
      />
    )
  } else if (mode === 'ask-to') {
    body = <Group heading="Ask whom" entries={visible(members.map((m) => ({ id: m.person, label: m.name, icon: <User />, hint: m.role, run: () => (setQ(''), setMode({ ask: m.person })) })))} />
  } else if (mode === 'move-pick') {
    body = (
      <Group
        heading="Move which ticket"
        entries={tickets.slice(0, 8).map((t) => ({
          id: t.key,
          label: (
            <>
              <span className="w-24 shrink-0 font-mono text-[12px] text-text-faint">{t.key}</span>
              <span className="flex-1 truncate">{t.title}</span>
              <span className="text-[11px] text-text-faint">{statusLabel(t.status, t.landing)}</span>
            </>
          ),
          run: () => (setQ(''), setMoveTarget({ key: t.key, status: t.status }), setMode('move')),
        }))}
      />
    )
  } else if (mode === 'move') {
    const target = moveTarget?.key ?? ticketKey!
    const cur = moveTarget ? moveTarget.status : ticket.data?.status
    body = (
      <Group
        heading={`Move ${target} to`}
        entries={visible(
          STATUSES.filter((s) => s !== cur && s !== 'done').map((s) => ({
            id: s,
            label: STATUS_LABEL[s],
            run: () => void post({ action: 'set_status', status: s }, `Moved ${target} to ${STATUS_LABEL[s]}`, target),
          })),
        )}
      />
    )
  } else {
    const commandsOnly = prefix === '>'
    body = (
      <>
        {/* A typed key or key prefix puts its ticket on the first row, above Recent, and it is the preselected one. */}
        {(prefix === '' || prefix === '#') && topRank <= 1 && <Group heading="Tickets" entries={ticketEntries} />}
        {!prefix && <Group heading="Recent" entries={visible(recentEntries)} />}
        {(!prefix || commandsOnly) && <Group heading="On this ticket" entries={visible(onTicket)} />}
        {(!prefix || commandsOnly) && <Group heading="On the board" entries={visible(onBoard)} />}
        {(prefix === '' || prefix === '#') && topRank > 1 && <Group heading="Tickets" entries={ticketEntries} />}
        {(!prefix || commandsOnly) && <Group heading="Go to" entries={visible(goTo)} />}
        {(!prefix || commandsOnly) && <Group heading="Create" entries={visible(create)} />}
        {(!prefix || commandsOnly) && <Group heading="Addon commands" entries={visible(addonCommands)} />}
        {prefix === '@' && <Group heading="People" entries={visible(people)} />}
        {(!prefix || commandsOnly) && <Group heading="Workspace" entries={visible(switchWs)} />}
      </>
    )
  }

  const placeholder = mode === 'quick' ? 'Quick ticket: describe it in one line, then Enter...' : mode === 'comment' ? 'Write a comment, then Enter...' : typeof mode === 'object' && mode ? 'Write the question, then Enter...' : mode === 'ask-to' ? 'Ask whom?' : mode === 'move' ? 'Move to which status?' : mode === 'move-pick' ? 'Search for the ticket to move...' : PLACEHOLDER

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
      {runAddon.dialog}
    </>
  )
}
