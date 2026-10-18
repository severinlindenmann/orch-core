// Model routing of a real host: which model each Start agent mode starts on, per agent. The lists come from the
// dashboard server (core's closed lists, `GET /model-routing`); the choice itself is a signed settings change
// (`models_set`, the typed word). Real host only (the mockup keeps its own `models` add-on).

import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'
import type { AgentMode, Harness } from './agents'

export interface ModelChoice {
  id: string
  label: string
}
export interface RoutingChoices {
  harnesses: { id: Harness; label: string; models: ModelChoice[] }[]
  modes: { id: AgentMode; label: string }[]
}
/** The signed table: agent -> mode -> model. A mode that is not named uses the agent's own default. */
export type ModelTable = Partial<Record<Harness, Partial<Record<AgentMode, string>>>>

export const routingApi = {
  choices: (ws: string) => call<RoutingChoices>('GET', `/api/w/${encodeURIComponent(ws)}/model-routing`),
}

export const routingQueries = {
  choices: (ws: string) => queryOptions({ queryKey: ['model-routing', ws], queryFn: () => routingApi.choices(ws), retry: false, staleTime: Infinity }),
}

/** The model's name for a person, or the agent default. */
export function modelLabel(choices: RoutingChoices | undefined, harness: Harness, id: string | null | undefined): string {
  if (!id) return 'the agent’s own default'
  return choices?.harnesses.find((h) => h.id === harness)?.models.find((m) => m.id === id)?.label ?? id
}

/** The models offered as a failing task's stronger choice, in order of preference (a suggestion only, never automatic). */
export const STRONG_MODELS: Partial<Record<Harness, string[]>> = { 'claude-code': ['fable', 'opus'], codex: ['gpt-5.4'] }

/** A table without empty cells, or `{}` when nothing is chosen (which clears the setting). */
export function cleanTable(t: ModelTable): ModelTable {
  const out: ModelTable = {}
  for (const [h, per] of Object.entries(t) as [Harness, Partial<Record<AgentMode, string>>][]) {
    const kept = Object.fromEntries(Object.entries(per ?? {}).filter(([, m]) => m)) as Partial<Record<AgentMode, string>>
    if (Object.keys(kept).length) out[h] = kept
  }
  return out
}
