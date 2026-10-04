import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Problem, Role, Task, Ticket } from '../types'
import { ICON, active, changes, loadTickets, ordered, serial, statusLine } from './tickets'

const PANE = 'orch-session'
const TIMEOUT_MS = 15000

// orch-core's design tokens (static/tokens.json, role fg): pink only for the human's move, blue for the agent at
// work, amber for a warning, red for an error, mint green for done. `neu` draws dim.
const PALETTE: Record<'dark' | 'light', Record<Role, string | undefined>> = {
  dark: { you: '#FF8FC4', info: '#9CC0FF', warn: '#F5B544', err: '#FF8A80', ok: '#A9F7C0', neu: undefined },
  light: { you: '#B8136A', info: '#1F5FD1', warn: '#7A4E00', err: '#B42318', ok: '#0B4F3D', neu: undefined },
}
let tone = PALETTE.dark

const WHO: Record<string, string> = { you: 'YOUR MOVE', agent: 'AGENT WORKING', nobody: '' }
const PROBLEM: Record<Problem, string> = {
  'no-workspace': 'No orch workspace here.',
  missing: 'orch is not on the PATH.',
  timeout: 'orch did not answer in time.',
  failed: 'orch reported an error.',
  unreadable: 'orch gave output this plugin cannot read.',
  outdated: 'This orch does not say whose move it is: update orch-core (schema 1.4.0 or newer).',
}

const tickets = atom({ plugin: 'orch-session', key: 'tickets' } as const, null)
const problem = atom({ plugin: 'orch-session', key: 'problem' } as const, null)

async function refreshOnce($: EngineInterface) {
  const env = { CLAUDE_CODE_SESSION_ID: await $.session.id() }
  const got = await loadTickets(argv => $.process.run(argv, { env, timeoutMs: TIMEOUT_MS }))
  if ('problem' in got) {
    // Keep the last whole state; without a workspace there is nothing to keep.
    await update($, problem, () => got.problem)
    if (got.problem === 'no-workspace') {
      await update($, tickets, () => null)
      $.ui.status(undefined)
    }
    return
  }
  const before = await read($, tickets)
  if (before) for (const text of changes(before, got.tickets)) $.ui.toast(text, { timeoutMs: 6000 })
  await update($, tickets, () => got.tickets)
  // an orch without `move`: the tickets show "Update orch-core" instead of a guess
  const outdated = got.tickets.some(t => t.move.what === 'outdated')
  await update($, problem, () => (outdated ? 'outdated' : null))
  $.ui.status(statusLine(got.tickets))
}

// One refresh at a time (serial), set up per session start.
let refresh: () => Promise<void> = () => Promise.resolve()

function count(t: Ticket): string {
  return t.total > 0 ? `${t.closed}/${t.total}` : '—'
}

const TASK_ROLE: Record<string, Role> = { doing: 'info', blocked: 'warn', done: 'ok', skipped: 'neu', todo: 'neu' }
const TASK_ICON: Record<string, string> = { doing: '◐', blocked: '▲', done: '✓', skipped: '–', todo: '○' }

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    refresh = serial(() => refreshOnce($))
    await $.command.register({ name: 'orch', description: 'Show the tickets this session holds' })
    try {
      const theme = (await $.config.list()).find(row => row.key === 'theme')?.value
      tone = String(theme ?? '').includes('light') ? PALETTE.light : PALETTE.dark
    } catch {
      // the dark palette
    }
    void refresh()
    // ponytail: polls every 20 s; watch orchestrator/.state/events.jsonl if that ever feels slow
    $.clock.every(20000, () => void refresh())
    return started
  })

  // An orch command the agent just ran is the moment something changed. The refresh runs on its own: the tool's
  // result never waits for it.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (/\borch\s/.test(e.command)) void refresh()
    return ran
  })

  on('command.run', { command: 'orch' }, async $ => {
    await $.ui.open({ id: PANE, title: 'orch · this session' })
    void refresh()
    return { text: 'orch pane opened.' }
  })

  // One row: the move that matters most right now.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, tickets)
    const t = list && active(list)
    if (e.props.hasSurvey || !list || !t) return next(e)
    const { Text } = $.ui.resolve(e)
    const m = t.move
    const others = list.length - 1
    return (
      <Text wrap="truncate-end">
        <Text color={tone[m.role]} dimColor={!tone[m.role]} bold>{ICON[m.role]} {t.id}</Text>{' '}
        <Text dimColor>{count(t)}</Text>{' '}
        <Text color={tone[m.role]} dimColor={!tone[m.role]}>
          {m.who === 'you' ? `Your move: ${m.label}` : m.label}
          {t.detail ? ` · ${t.detail}` : ''}
        </Text>
        {others > 0 ? <Text dimColor> · +{others} more, /orch</Text> : ''}
      </Text>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Link, Text } = $.ui.resolve(e)
    const list = await read($, tickets)
    const trouble = await read($, problem)
    // A failed read is an error (the last good state stays); an orch too old to say whose move is a warning.
    const bannerRole: Role = trouble === 'outdated' ? 'warn' : 'err'
    const banner = trouble && trouble !== 'no-workspace' && (
      <Text color={tone[bannerRole]} wrap="wrap">
        {ICON[bannerRole]} {PROBLEM[trouble]}{list && trouble !== 'outdated' ? ' Showing the last good state.' : ''}
      </Text>
    )
    if (!list || list.length === 0) {
      return (
        <Box flexDirection="column">
          {banner}
          <Text dimColor>
            {ICON.neu} {trouble === 'no-workspace' ? PROBLEM['no-workspace'] : 'This session holds no ticket.'}
          </Text>
        </Box>
      )
    }

    const taskLine = (task: Task) => {
      const role = TASK_ROLE[task.state] ?? 'neu'
      const closed = task.state === 'done' || task.state === 'skipped'
      return (
        <Text key={task.id} color={closed ? undefined : tone[role]} dimColor={closed || !tone[role]} wrap="truncate-end">
          {TASK_ICON[task.state] ?? '○'} {task.id} {task.state} · {task.text}
          {task.owner === 'human' ? ' (yours)' : ''}
          {task.state === 'blocked' && task.why ? ` — ${task.why}` : ''}
        </Text>
      )
    }

    // One card per ticket: whose move, the one action, the work, the PR.
    return (
      <Box flexDirection="column">
        {banner}
        {list.map(t => {
          const m = t.move
          const color = tone[m.role]
          const who = WHO[m.who] || m.label.toUpperCase()
          return (
            <Box
              key={t.id}
              flexDirection="column"
              borderStyle="round"
              borderColor={color}
              borderDimColor={!color}
              paddingX={1}
              marginBottom={1}
            >
              <Box justifyContent="space-between">
                <Text>
                  <Text color={color} dimColor={!color} bold>{ICON[m.role]} {t.id} {who}</Text>
                  <Text dimColor> · {t.status}</Text>
                </Text>
                <Text dimColor>{count(t)}</Text>
              </Box>
              <Text bold wrap="truncate-end">{t.title}</Text>
              {t.epic && <Text dimColor wrap="truncate-end">in epic {t.epic}</Text>}
              <Text color={color} dimColor={!color} wrap="wrap">
                {m.label}{t.detail ? `: ${t.detail}` : ''}
              </Text>
              {t.question && t.question.options.map(o => (
                <Text key={o.key} dimColor wrap="truncate-end">
                  {'  '}{o.key} {o.label}{o.key === t.question?.recommended ? ' (recommended)' : ''}
                </Text>
              ))}
              {t.question && (
                <Text dimColor wrap="wrap">{'  '}In your own terminal: {t.question.command}</Text>
              )}
              {t.confirm.length > 0 && (
                <Text dimColor wrap="truncate-end">
                  {ICON.neu} {t.confirm.join(', ')}: the agent went ahead on its recommendation; confirm when you can
                </Text>
              )}
              {ordered(t.tasks).map(taskLine)}
              {t.verdict && t.status !== 'testing' && (
                <Text dimColor wrap="truncate-end">last verdict: {t.verdict}</Text>
              )}
              {t.pr && (
                <Text dimColor wrap="truncate-end">
                  <Link href={t.pr.url}>{`${t.pr.label} ↗`}</Link>
                </Text>
              )}
            </Box>
          )
        })}
      </Box>
    )
  })
}
