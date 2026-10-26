// Today's "Re-login needed" items (D57): one per connection whose last check is auth expired or wrong identity, for
// the owner only. It shows the connection's login_hint to copy, "Log in in the terminal" (a shell as the OS user that
// runs the agents with the command typed, not run; only with the terminals addon holding pty) and "Run check again";
// agents never handle logins and no password is ever stored.
import { reloginItems } from '@/api/attention'
import { ChevronDown, KeyRound, Loader2, RefreshCw, SquareTerminal } from 'lucide-react'
import { useState } from 'react'
import { CHECK_LABEL, type ConnectionInfo } from '@/api/connections'
import { can } from '@/api/permissions'
import { useAddons } from '@/addon-ui/slots'
import { useRunAddonAction } from '@/addon-ui/useRunAddonAction'
import { canUsePty } from '@/addon-ui/capabilities'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { CopyButton } from '../ticket/Header'
import { checkTime, DEMO_RELOGIN, DemoChip, PhaseChip, useConnections } from '../settings/connectionUi'
import { useRunCheck } from '../settings/Connections'
import { RowShell } from './rows'

/** Terminals holds pty here (enabled, granted for the installed version) and the viewer may open the login shell. */
function useLoginShell() {
  const { workspace } = useWorkspace()
  const { data: packages } = useAddons()
  const run = useRunAddonAction()
  const ok = canUsePty(packages?.find((a) => a.name === 'terminals'), workspace?.addons.terminals) && run.allowed('terminals', 'login_shell')
  return { ok, run, open: (connection: string) => run.run('terminals', 'login_shell', { connection }, connection) }
}

function ReloginRow({ c, ws, now, expanded, onToggle }: { c: ConnectionInfo; ws: string; now: string; expanded: boolean; onToggle: () => void }) {
  const check = useRunCheck(ws)
  const shell = useLoginShell()
  const last = c.last_check!
  const blocks = c.tickets.length ? ` · blocks ${c.tickets.join(', ')}` : ''
  const why = last.status === 'wrong_identity' ? `wrong identity: expected ${last.expected}, got ${last.actual}` : CHECK_LABEL[last.status]
  return (
    <RowShell
      testId={`relogin-${c.name}`}
      icon={<KeyRound className="size-4 text-danger" aria-hidden />}
      ask={`Re-login needed: ${c.name}`}
      sub={`${why}${blocks} · checked ${checkTime(last.at, now)}`}
      expanded={expanded}
      onToggle={onToggle}
      action={
        c.kind === 'cli_login' ? (
          <>
            {shell.ok && c.login_hint && (
              <Button size="icon-sm" variant="outline" aria-label={`Log in to ${c.name} in the terminal (as ${c.run_as})`} title="Log in in the terminal" disabled={shell.run.pending} onClick={() => shell.open(c.name)}>
                <SquareTerminal />
              </Button>
            )}
            <Button size="sm" variant="outline" disabled={check.isPending} onClick={() => check.mutate({ name: c.name, trigger: 'relogin' })}>
              {check.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              Run check again
            </Button>
          </>
        ) : (
          <Button size="sm" variant="outline" disabled={check.isPending} onClick={() => check.mutate({ name: c.name })}>
            <RefreshCw />
            Run check again
          </Button>
        )
      }
    >
      {c.kind === 'cli_login' && c.login_hint ? (
        <>
          <p className="text-[13px] text-text-muted">
            Log in again in a terminal as <span className="font-mono text-text">{c.run_as}</span> (the user that runs the agents), then run the check again. orch never sees the password or copies the token; {c.tool} keeps its own.
          </p>
          <span className="flex items-center gap-1">
            <code className="min-w-0 break-all rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[12px]">{c.login_hint}</code>
            <CopyButton text={c.login_hint} label={`Copy the login command for ${c.name}`} />
          </span>
          {shell.ok && (
            <div className="flex flex-wrap items-center gap-2">
              <Button size="xs" variant="outline" disabled={shell.run.pending} onClick={() => shell.open(c.name)}>
                <SquareTerminal />
                Log in in the terminal
              </Button>
              <p className="min-w-0 text-[12px] text-text-muted">
                Opens a shell as <span className="font-mono text-text">{c.run_as}</span> with this command typed. Press Enter to run it yourself, then run the check again.
              </p>
            </div>
          )}
          <p className="flex items-center gap-2 text-[12px] text-text-muted">
            <DemoChip />
            {DEMO_RELOGIN} No login happens in this mockup.
          </p>
        </>
      ) : (
        <p className="text-[13px] text-text-muted">Update {c.env.join(', ')} in the secrets file on the host, then run the check again. Running sessions keep the old value until restarted.</p>
      )}
      <p className="flex items-center gap-2 text-[12px] text-text-faint">
        Agents never handle logins. The same prompt on your phone comes later. <PhaseChip phase="P4" />
      </p>
      {shell.run.dialog}
    </RowShell>
  )
}

/** The owner's re-login items, as a group above the queue. Nothing for anyone else. */
export function ReloginGroup({ now }: { now: string }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const owner = can(role, 'settings')
  const connections = useConnections(owner ? ws : undefined)
  const [open, setOpen] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const items = reloginItems(connections.data ?? [])
  if (!owner || !ws || items.length === 0) return null
  return (
    <section role="region" aria-labelledby="today-group-relogin" className="overflow-hidden rounded-lg border border-border bg-surface">
      <h2 className="text-[13px]">
        <button
          id="today-group-relogin"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-1 px-3 py-2.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <ChevronDown className={cn('mr-1 size-4 shrink-0 text-text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
          <span className="font-semibold text-text">Connections</span>{' '}
          <span className="min-w-0 truncate tabular-nums text-text-muted">· {items.length} · only the owner logs in again</span>
        </button>
      </h2>
      {open && (
        <ul className="border-t border-border">
          {items.map((c) => (
            <ReloginRow key={c.name} c={c} ws={ws} now={now} expanded={expanded === c.name} onToggle={() => setExpanded(expanded === c.name ? null : c.name)} />
          ))}
        </ul>
      )}
    </section>
  )
}
