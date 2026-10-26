// The models a start can name, per agent. The lists come from the dashboard server (core's closed lists,
// `GET /model-routing`). Which model an add-on that holds `choose_model` picks is core's answer in the start preview.

import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'
import { isRealHost } from './features'
import type { Harness } from './agents'

export interface ModelChoice {
  id: string
  label: string
}
export interface RoutingChoices {
  harnesses: { id: Harness; label: string; models: ModelChoice[] }[]
}

// The mock host serves no `/model-routing`: the dev server answered that path with its HTML page, a "success" without
// `harnesses`, and the start dialog crashed reading it. Mock mode answers with the closed demo list instead, and a
// reply of any other shape is an error, never data.
const DEMO_CHOICES: RoutingChoices = {
  harnesses: [
    { id: 'claude-code', label: 'Claude Code', models: [{ id: 'sonnet', label: 'Sonnet' }, { id: 'opus', label: 'Opus' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }] },
  ],
}

export const routingApi = {
  choices: async (ws: string): Promise<RoutingChoices> => {
    if (!isRealHost()) return DEMO_CHOICES
    const got = await call<RoutingChoices>('GET', `/api/w/${encodeURIComponent(ws)}/model-routing`)
    if (!got || !Array.isArray(got.harnesses)) throw new Error('model-routing: unexpected reply')
    return got
  },
}

export const routingQueries = {
  choices: (ws: string) => queryOptions({ queryKey: ['model-routing', ws], queryFn: () => routingApi.choices(ws), retry: false, staleTime: Infinity }),
}

/** The model's name for a person, or the agent default. */
export function modelLabel(choices: RoutingChoices | undefined, harness: Harness, id: string | null | undefined): string {
  if (!id) return 'the agent’s own default'
  return choices?.harnesses?.find((h) => h.id === harness)?.models.find((m) => m.id === id)?.label ?? id
}

/** The models offered as a failing task's stronger choice, in order of preference (a suggestion only, never automatic). */
export const STRONG_MODELS: Partial<Record<Harness, string[]>> = { 'claude-code': ['fable', 'opus'], codex: ['gpt-5.4'] }
