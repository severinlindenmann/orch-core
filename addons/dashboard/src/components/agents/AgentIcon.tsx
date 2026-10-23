// The coloured mark of an agent app (Claude Code: terracotta asterisk, Codex: teal brackets), and the mark with its name.
// Shared by every place that names an agent, so the same agent always looks the same.
import { HARNESSES, type Harness } from '@/api/agents'
import { cn } from '@/lib/utils'

const NAME = (id: string): string => HARNESSES.find((h) => h.id === id)?.name ?? id

export function AgentIcon({ harness, className, size = 20 }: { harness: Harness | string; className?: string; size?: number }) {
  const codex = harness === 'codex'
  return (
    <svg aria-hidden="true" data-agent-icon={harness} viewBox="0 0 24 24" width={size} height={size} className={cn('shrink-0', className)}>
      <rect width="24" height="24" rx="6" fill={codex ? '#061e1c' : '#b96648'} />
      {codex ? <path d="m8 7-4 5 4 5m8-10 4 5-4 5m-3-11-2 12" stroke="#2fc6a3" fill="none" strokeWidth="1.6" /> : <path d="M12 4v16M4 12h16M6 6l12 12M6 18 18 6" stroke="#fff0dc" strokeWidth="1.6" />}
    </svg>
  )
}

/** The mark and the name, side by side (a name never appears without its mark). */
export function AgentName({ harness, className }: { harness: Harness | string; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 align-middle', className)}>
      <AgentIcon harness={harness} size={18} />
      <span>{NAME(harness)}</span>
    </span>
  )
}
