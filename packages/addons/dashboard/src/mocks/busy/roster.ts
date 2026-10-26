// The agent sessions that are working in the busy day, across Severin's and Mara's grants (DEMO workspace).
// Their ids never clash with the fixtures (s_77c2, s_9e3f, s_a41, ...).
export interface Session {
  id: string
  /** The root session (a subagent `s_b101.1` has root `s_b101`). */
  root: string
  for: 'p_sev' | 'p_mara'
  agent: 'claude-code' | 'codex'
  name: string
  model: string
  parent: string | null
}

export const GRANT_OF: Record<Session['for'], { id: string; until: string }> = {
  p_sev: { id: 'gr_01J9Z8', until: '2026-10-09T18:00:00Z' },
  p_mara: { id: 'gr_01J9Y4', until: '2026-10-09T17:00:00Z' },
}

const root = (id: string, forPerson: Session['for'], agent: Session['agent'], model: string): Session => ({
  id, root: id, for: forPerson, agent, name: agent === 'codex' ? 'Codex' : 'Claude Code', model, parent: null,
})
const sub = (parent: string, n: number, forPerson: Session['for'], agent: Session['agent'], model: string): Session => ({
  id: `${parent}.${n}`, root: parent, for: forPerson, agent, name: agent === 'codex' ? 'Codex' : 'Claude Code', model, parent,
})

export const SESSIONS: Session[] = [
  root('s_b101', 'p_sev', 'claude-code', 'claude-opus-4-5'),
  sub('s_b101', 1, 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  sub('s_b101', 2, 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  sub('s_b101', 3, 'p_sev', 'claude-code', 'claude-haiku-4-5'),
  root('s_b102', 'p_sev', 'claude-code', 'claude-opus-4-5'),
  sub('s_b102', 1, 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  sub('s_b102', 2, 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  root('s_b103', 'p_sev', 'codex', 'gpt-5-codex'),
  root('s_b104', 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  sub('s_b104', 1, 'p_sev', 'claude-code', 'claude-haiku-4-5'),
  root('s_b105', 'p_sev', 'claude-code', 'claude-opus-4-5'),
  root('s_b106', 'p_sev', 'claude-code', 'claude-sonnet-4-5'),
  root('s_c201', 'p_mara', 'codex', 'gpt-5-codex'),
  sub('s_c201', 1, 'p_mara', 'codex', 'gpt-5-codex'),
  sub('s_c201', 2, 'p_mara', 'codex', 'gpt-5-codex'),
  root('s_c202', 'p_mara', 'claude-code', 'claude-opus-4-5'),
  root('s_c203', 'p_mara', 'codex', 'gpt-5-codex'),
  sub('s_c203', 1, 'p_mara', 'codex', 'gpt-5-codex'),
  root('s_c204', 'p_mara', 'claude-code', 'claude-sonnet-4-5'),
]

export const ROOTS = SESSIONS.filter((s) => !s.parent)
export const actorOf = (s: Pick<Session, 'agent' | 'id' | 'for'>) => `${s.agent}:${s.id}:${s.for}`
export const subsOf = (rootId: string) => SESSIONS.filter((s) => s.parent === rootId)

/** Sessions that have ended: the history of finished tickets is attributed to them. */
export const ENDED: Pick<Session, 'agent' | 'id' | 'for'>[] = [
  { id: 's_4f10', agent: 'claude-code', for: 'p_sev' },
  { id: 's_5d10', agent: 'claude-code', for: 'p_sev' },
  { id: 's_6a22', agent: 'claude-code', for: 'p_sev' },
  { id: 's_7b31', agent: 'codex', for: 'p_mara' },
  { id: 's_8c40', agent: 'codex', for: 'p_mara' },
  { id: 's_9d55', agent: 'claude-code', for: 'p_mara' },
]
