import { Asterisk, CodeXml, Bot } from 'lucide-react'
import { findHarness, harnessForAgent } from '@/api/harnesses'
import { cn } from '@/lib/utils'

/** The coloured tile of a coding agent, so Claude Code and Codex are told apart at a glance. The name always travels with it. */
const MARKS: Record<string, { tile: string; Icon: typeof Bot }> = {
  claude: { tile: 'bg-[#c96442] text-white', Icon: Asterisk },
  codex: { tile: 'bg-[#0f9d78] text-white', Icon: CodeXml },
}

/** `agent`: an orch agent id ("claude-code", "codex") or a harness id ("claude"). An agent this dashboard does not know gets a neutral tile. */
export function AgentMark({ agent, className }: { agent: string; className?: string }) {
  const id = harnessForAgent(agent)
  const { tile, Icon } = MARKS[id] ?? { tile: 'bg-surface-3 text-text-muted', Icon: Bot }
  return (
    <span aria-hidden className={cn('inline-grid size-5 shrink-0 place-items-center rounded-md', tile, className)}>
      <Icon className="size-3.5" strokeWidth={2.4} />
    </span>
  )
}

/** The mark with its name: "Claude Code". */
export function AgentName({ agent }: { agent: string }) {
  const label = findHarness(harnessForAgent(agent))?.label ?? 'An agent'
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px]">
      <AgentMark agent={agent} />
      {label}
    </span>
  )
}
