// The agent picker shared by the Start agent dialog and the New ticket form: which agent app, what it does, which
// model. Rows of a two-column grid (label | control), so each caller lays them out in its own grid.
import type { ReactNode } from 'react'
import { AGENT_MODES, HARNESSES, type AgentMode, type Harness } from '@/api/agents'
import type { ModelChoice } from '@/api/modelRouting'
import { Label } from '@/components/ui/label'

/** One plain line per mode, shown under the choice. */
export const MODE_HELP: Record<AgentMode, string> = {
  refine: 'Helps you write the requirements and the plan. It changes no code.',
  work: 'Works through the ticket: writes the code and submits it for review.',
  fix: 'Fixes what a reviewer or a check sent back.',
  continue: 'Picks up where an earlier agent stopped.',
  context: 'Reads the ticket and the code, so you can ask questions. It changes nothing.',
}

/** What each mode is called on screen; the developer word stays in the option's Details line. */
export const MODE_LABEL: Record<AgentMode, string> = {
  refine: 'Write requirements and plan',
  work: 'Do the work',
  fix: 'Fix what was sent back',
  continue: 'Continue earlier work',
  context: 'Read and answer questions',
}

export const harnessName = (h: Harness): string => HARNESSES.find((x) => x.id === h)?.name ?? h

const SELECT = 'h-9 min-w-0 rounded border border-border bg-bg px-2 sm:h-7'

/** Agent app and what it does (with the one-line help). Renders grid rows; the parent supplies a two-column grid. */
export function AgentPick({ harness, mode, onHarness, onMode, idPrefix = 'agent' }: { harness: Harness; mode: AgentMode; onHarness: (h: Harness) => void; onMode: (m: AgentMode) => void; idPrefix?: string }): ReactNode {
  return (
    <>
      <Label htmlFor={`${idPrefix}-harness`} className="text-xs text-text-muted">Agent</Label>
      <select id={`${idPrefix}-harness`} className={SELECT} value={harness} onChange={(e) => onHarness(e.target.value as Harness)}>
        {HARNESSES.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
      </select>
      <Label htmlFor={`${idPrefix}-mode`} className="text-xs text-text-muted">What it does</Label>
      <select id={`${idPrefix}-mode`} className={SELECT} value={mode} onChange={(e) => onMode(e.target.value as AgentMode)} aria-describedby={`${idPrefix}-mode-help`}>
        {AGENT_MODES.map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
      </select>
      <span className="hidden sm:block" />
      <div id={`${idPrefix}-mode-help`} data-testid={`${idPrefix}-mode-help`} className="-mt-1 text-text-muted">{MODE_HELP[mode]} <details className="inline"><summary className="inline cursor-pointer">Details</summary> Mode name for developers: <code>{mode}</code></details></div>
    </>
  )
}

/** The model of the agent: empty = the host picks (from Settings when model routing is on). Grid rows as well. */
export function ModelPick({ models, model, onModel, emptyLabel = 'As set for this kind of work', idPrefix = 'agent' }: { models: ModelChoice[]; model: string; onModel: (m: string) => void; emptyLabel?: string; idPrefix?: string }): ReactNode {
  return (
    <>
      <Label htmlFor={`${idPrefix}-model`} className="text-xs text-text-muted">Model</Label>
      <select id={`${idPrefix}-model`} className={SELECT} value={model} onChange={(e) => onModel(e.target.value)}>
        <option value="">{emptyLabel}</option>
        {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
      </select>
    </>
  )
}
