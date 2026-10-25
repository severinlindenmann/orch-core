import { ShieldCheck } from 'lucide-react'
import { cn } from '@/lib/utils'

/** D-P2-31, the card line (core words): the New terminal and Start agent dialogs, whenever execution is on. */
export const EXECUTION_LINE = 'Dashboard execution: agents and terminals started here run as you. Anything that takes over this dashboard, or any agent it starts, can run commands and read your files and keys.'

/** Tier B (host spec 13.5): the server's card line names the separate account a terminal or agent runs as. */
const TIER_B_LINE = /^This terminal runs as (orch-agent-[a-z_][a-z0-9_-]{0,31})\./

/** The account name when the host's preview carries the tier B line, else null (the page keeps the "run as you" words). */
export function separateAccount(line: string | null | undefined): string | null {
  const m = typeof line === 'string' ? TIER_B_LINE.exec(line) : null
  return m ? m[1] : null
}

/** The plain "separate account" line with a shield: reassurance, so neutral colours (variant A of the R1-8 drafts). */
export function SeparateAccountLine({ user, what = 'terminal', className }: { user: string; /** The dialog's own word: a terminal, or an agent. */ what?: 'terminal' | 'agent'; className?: string }) {
  return (
    <p data-testid="separate-account-line" className={cn('flex items-start gap-2 text-xs leading-5 text-text-muted', className)}>
      <ShieldCheck aria-hidden className="mt-0.5 size-4 shrink-0 text-brand" />
      <span>This {what} uses a <b className="font-semibold text-text">separate account</b> (<b className="font-semibold text-text">{user}</b>). It cannot read your orch signing keys or passkeys.</span>
    </p>
  )
}
