// The models a start can name, per agent. The lists come from the dashboard server (core's closed lists,
// `GET /model-routing`). Which model an add-on that holds `choose_model` picks is core's answer in the start preview.

import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'
import type { Harness } from './agents'

export interface ModelChoice {
  id: string
  label: string
}
export interface RoutingChoices {
  harnesses: { id: Harness; label: string; models: ModelChoice[] }[]
}

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
