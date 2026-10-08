// Core-rendered terminal (xterm.js over the fake PTY). Rendered only through the capability-gated `terminal` node
// (src/addon-ui/AddonNode.tsx). The session is looked up in the addon's own state for THIS workspace and viewer: an
// unknown session renders the fallback. Input is allowed only when the host says the session is interactive for this
// viewer AND the viewer is a member or above AND the viewer is the owner; everything else is view-only (stdin disabled,
// aria-readonly). Agents never get a PTY: their sessions are mirrors replayed from a transcript.
import '@xterm/xterm/css/xterm.css'
import { Terminal } from '@xterm/xterm'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, type ReactNode } from 'react'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { TerminalSessionView } from '@/api/terminals'
import { useAddonStates } from '@/addon-ui/slots'
import { Skeleton } from '@/components/ui/skeleton'
import { useRole } from '../useRole'
import { useWorkspace } from '../workspace'
import { createShell, replay } from './fakePty'

const token = (el: HTMLElement, name: string) => getComputedStyle(el).getPropertyValue(name).trim() || undefined

export default function TerminalView({ addon, session, fallback }: { addon: string; session: string; fallback: ReactNode }) {
  const { workspace } = useWorkspace()
  const { [addon]: state } = useAddonStates(workspace?.id, [addon])
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const role = useRole()
  if (!workspace || !state || !me.data || !role) return <Skeleton className="h-64 w-full" />
  const s = (state.sessions as TerminalSessionView[] | undefined)?.find((x) => x.id === session)
  if (!s) return <>{fallback}</>
  const interactive = s.interactive && s.kind === 'person' && s.owner === me.data.person && can(role, 'addon.action')
  const fontSize = Number((state.settings as { font_size?: number } | undefined)?.font_size) || 13
  return <XtermSession key={`${s.id}:${interactive}`} session={s} interactive={interactive} fontSize={fontSize} />
}

function XtermSession({ session, interactive, fontSize }: { session: TerminalSessionView; interactive: boolean; fontSize: number }) {
  const host = useRef<HTMLDivElement>(null)
  // The shell reads the newest host data (grant, claim, cursor) when a command runs.
  const ctx = useRef(session.ctx)
  ctx.current = session.ctx

  useEffect(() => {
    const el = host.current
    if (!el) return
    const term = new Terminal({
      cols: 96,
      rows: 14,
      fontSize,
      fontFamily: 'Geist Mono, ui-monospace, monospace',
      cursorBlink: interactive,
      disableStdin: !interactive,
      convertEol: false,
      theme: { background: token(el, '--bg'), foreground: token(el, '--text'), cursor: token(el, '--brand') },
    })
    term.open(el)
    if (!interactive) {
      term.textarea?.setAttribute('aria-readonly', 'true')
      term.write(replay(ctx.current, session.transcript))
      if (session.status === 'stopped') term.write('\x1b[2m[process completed]\x1b[0m\r\n')
      return () => term.dispose()
    }
    const shell = createShell(() => ctx.current)
    term.write(shell.prompt())
    const sub = term.onData((d) => {
      const out = shell.feed(d)
      if (out) term.write(out)
      if (shell.exited()) term.write('\x1b[2m[process completed]\x1b[0m\r\n')
    })
    return () => {
      sub.dispose()
      term.dispose()
    }
  }, [session.id, session.status, session.transcript, interactive, fontSize])

  return (
    <div
      ref={host}
      role="group"
      aria-label={`Terminal: ${session.label}`}
      aria-readonly={interactive ? undefined : true}
      data-terminal-session={session.id}
      className="overflow-x-auto rounded-md border border-border bg-bg p-2"
    />
  )
}
