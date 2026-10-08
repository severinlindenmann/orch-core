import type { GenEvent } from './timeline'

/** Ticket definition in fixture shape (src/mocks/fixtures/demo.json): the store fills the rest with defaults. */
export interface GenDefinition {
  uid: string
  key: string
  title: string
  type: 'feature' | 'bug' | 'chore' | 'spike' | 'epic'
  priority: 'low' | 'medium' | 'high' | 'urgent'
  size: 'xs' | 's' | 'm' | 'l' | 'xl' | null
  labels: string[]
  parent: string | null
  visibility?: { restricted: string[] }
  links: { repos: string[]; branches: Record<string, string>; prs: { repo: string; url: string }[]; external: { label: string; url: string }[] }
  acceptance: { id: string; text: string }[]
  tasks: { id: string; text: string; verify: { cmd: string } | null; proves: string[]; assignee?: string }[]
  questions: {
    id: string
    to: string
    text: string
    why?: string
    options?: { key: string; label: string; cost?: string }[]
    recommended?: string
    blocking: boolean
  }[]
  addons: Record<string, Record<string, unknown>>
}

export interface GenTicket {
  definition: GenDefinition
  body: Record<string, string>
  events: GenEvent[]
}

/** The ticket archetypes of the busy day; each one is a different shape of work in progress. */
export type Arch =
  | 'epic'
  | 'done'
  | 'testSev'
  | 'testMara'
  | 'testBoth'
  | 'testNobody'
  | 'testFailed'
  | 'qSev'
  | 'qOwner'
  | 'qBoth'
  | 'qMara'
  | 'qAssignee'
  | 'reqPending'
  | 'planPending'
  | 'wip'
  | 'openReady'
  | 'backlogNew'
  | 'waitingExt'

/** A generated ticket with what the later passes (artifacts, widgets, agents) need to know about it. */
export interface Built extends GenTicket {
  arch: Arch
  /** Who does the work on it (an ended session, a person or the live session that holds the claim). */
  worker: string
  /** The live session that holds the claim, if any. */
  holder: string | null
  /** Tasks that are done. */
  doneTasks: string[]
  /** A question an agent waits for: the registry marks that session as waiting. */
  waitingOn: { question: string } | null
}

export interface WsCfg {
  prefix: 'DEMO' | 'INT' | 'CLI'
  /** First ticket number. */
  first: number
  /** Tickets to generate, epics included. */
  count: number
  /** The workspace owner (approves requirements and plans) and the second person. */
  owner: string
  other: string
  /** Live agent sessions exist (DEMO only). */
  agents: boolean
  /** Visibility lists handed out to restricted tickets, in order. */
  restricted: string[][]
  /** Children of each new epic. */
  epics: number[]
  /** An existing epic that gets extra generated children (the factory epic). */
  extraEpic?: { key: string; children: number }
}
