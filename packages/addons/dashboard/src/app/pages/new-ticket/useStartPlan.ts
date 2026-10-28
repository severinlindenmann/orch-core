// What "Create and start agent" will do, as the form knows it before anything is signed: whether starting is possible
// here, the agent choice (remembered per workspace), and whether the ticket's signature also has to carry agent access.
import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AGENT_MODES, HARNESSES, agentsApi, runnerQuery, type AgentMode, type Harness } from '@/api/agents'
import { hasFeature, handoffMode } from '@/api/features'
import { GRANT_MAX_HOURS, grantDefaultHours, runnerLengths, reusableAccess } from '@/api/grants'
import { routingQueries } from '@/api/modelRouting'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { nowMs } from '@/lib/time'
import { DEFAULT_CHOICE, shownModel, type AgentChoiceValue, type Reviewed } from './startAfterCreate'

export const agentPrefKey = (person: string, ws: string) => `orch.dashboard.new-ticket.agent.${person}.${ws}`

export interface AgentPref extends AgentChoiceValue {
  /** The default button: true = "Create and start agent", false = "Only create". */
  start: boolean
  /** The person picked the kind of work themselves: only then is it remembered over the default. */
  modeChosen?: boolean
}

/** A new ticket starts with writing the requirements and the plan; "Do the work" comes after they are approved. */
export const NEW_TICKET_MODE: AgentMode = 'refine'

/** The remembered choice; anything malformed is the default (never an error). */
export function readPref(key: string): AgentPref {
  const base: AgentPref = { ...DEFAULT_CHOICE, mode: NEW_TICKET_MODE, start: true }
  try {
    const raw = JSON.parse(localStorage.getItem(key) ?? 'null') as Record<string, unknown> | null
    if (!raw || typeof raw !== 'object') return base
    return {
      start: raw.start !== false,
      harness: HARNESSES.some((h) => h.id === raw.harness) ? (raw.harness as Harness) : base.harness,
      // A remembered kind of work counts only when the person chose it (an older save without the flag counts when it is
      // not the former default, which nobody could tell from a choice).
      mode: AGENT_MODES.includes(raw.mode as AgentMode) && (raw.modeChosen === true || (raw.modeChosen === undefined && raw.mode !== DEFAULT_CHOICE.mode)) ? (raw.mode as AgentMode) : base.mode,
      modeChosen: raw.modeChosen === true || (raw.modeChosen === undefined && AGENT_MODES.includes(raw.mode as AgentMode) && raw.mode !== DEFAULT_CHOICE.mode),
      model: typeof raw.model === 'string' && raw.model.length <= 100 ? raw.model : '',
    }
  } catch {
    return base
  }
}
function writePref(key: string, p: AgentPref) {
  try {
    localStorage.setItem(key, JSON.stringify(p))
  } catch { /* storage unavailable: the choice is just not remembered */ }
}

export function useStartPlan(workspace: Workspace, person: string) {
  const ws = workspace.id
  const prefKey = agentPrefKey(person, ws)
  const feature = handoffMode(ws) && hasFeature('agent_start', ws)
  const routing = hasFeature('model_routing', ws)
  const runner = useQuery({ ...runnerQuery(ws), enabled: feature, retry: false })
  const settings = useQuery({ ...queries.hostSettings(ws), enabled: feature, retry: false })
  const choices = useQuery({ ...routingQueries.choices(ws), enabled: feature && routing })
  // The "runs as you" notice (the Start agent dialog's cache): until it has been read once, the review shows it in full
  // and signing with it on screen counts as reading it. A failed read counts as not read.
  const notices = useQuery({ queryKey: ['risk-notices', ws], queryFn: () => agentsApi.riskNotices(ws), retry: false, enabled: feature })
  const noticeUnread = notices.data?.acknowledged.start_agent !== true
  const [pref, setPrefState] = useState<AgentPref>(() => readPref(prefKey))
  const setPref = useCallback((p: Partial<AgentPref>) => setPrefState((old) => { const next = { ...old, ...p, ...(p.mode !== undefined ? { modeChosen: true } : {}) }; writePref(prefKey, next); return next }), [prefKey])

  const info = runner.data?.runner ?? null
  const reason = !feature ? null
    : runner.isPending ? 'checking'
      : !info ? 'No agent runner is set up here.'
        : !info.enabled ? 'Dashboard execution is turned off here.'
          : !info.connected ? 'No agent runner is connected.'
            : null
  const available = feature && !reason
  // Startable means 20 minutes left: that changes with time alone, so it is read again every 30 s and, at submit, afresh.
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (!feature) return
    const t = setInterval(() => setTick((n) => n + 1), 30_000)
    return () => clearInterval(t)
  }, [feature])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  // the stopwatch: the page clock when this read of the host arrived (only differences of it are used)
  const readAt = useMemo(() => nowMs(), [info]) // eslint-disable-line react-hooks/exhaustive-deps
  const grant = useMemo(() => reusableAccess(info, nowMs() - readAt), [info, readAt, tick])
  const grantNow = useCallback(() => reusableAccess(info, nowMs() - readAt), [info, readAt])
  const needsGrant = available && !grant

  // Access length: the workspace maximum caps it; 1, 3 or 7 days (a workspace that allows less offers its own maximum).
  const hostCap = settings.data?.grant_hours
  const cap = typeof hostCap === 'number' ? Math.min(GRANT_MAX_HOURS, Math.max(1, Math.floor(hostCap))) : grantDefaultHours(workspace)
  const all = runnerLengths(cap)
  const lengths = all
  const [hoursChoice, setHours] = useState(8)
  const hours = lengths.some(([h]) => h === hoursChoice) ? hoursChoice : (lengths[0]?.[0] ?? 24)

  const choice: AgentChoiceValue = { harness: pref.harness, mode: pref.mode, model: pref.model }
  const models = choices.data?.harnesses.find((h) => h.id === pref.harness)?.models ?? []
  const shown = shownModel(choice)
  const reviewed: Reviewed = { choice, model: shown, grantId: grant?.id ?? null }
  return { feature, available, reason: reason === 'checking' ? null : reason, pending: feature && runner.isPending, grant, grantNow, needsGrant, lengths, hours, setHours, cap, pref, setPref, choice, models, routing, choices: choices.data, shown, reviewed, noticeUnread, priorGrantIds: info?.grant ? [info.grant.id] : [] }
}
export type StartPlan = ReturnType<typeof useStartPlan>
