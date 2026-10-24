// What a quick task row says and what its one button does (item 4.4): a status in the person's words and the matching action.
import type { QuickRow } from '@/api/quickRecords'

export type TaskAction = 'start' | 'review' | 'answer' | 'retry' | null
export interface TaskState {
  word: string
  tone: 'quiet' | 'live' | 'attention' | 'bad'
  action: TaskAction
  actionLabel: string | null
}

/** Pure: the row's words from what the host says about the ticket, plus what this page knows (a start it is making or that failed). */
export function taskState(row: QuickRow, o: { starting?: boolean; failed?: boolean } = {}): TaskState {
  if (row.status === 'done' || row.status === 'closed') return { word: 'Finished', tone: 'quiet', action: null, actionLabel: null }
  if (o.starting) return { word: 'Starting agent…', tone: 'live', action: null, actionLabel: null }
  if (o.failed) return { word: 'Couldn’t start', tone: 'bad', action: 'retry', actionLabel: 'Try again' }
  if (row.open_questions > 0) return { word: 'Needs your answer', tone: 'attention', action: 'answer', actionLabel: 'Answer' }
  if (row.claim && row.claim.live !== false) return { word: 'Working', tone: 'live', action: null, actionLabel: null }
  if (row.awaiting_gate === 'verify' || row.status === 'testing') return { word: 'Ready for your review', tone: 'attention', action: 'review', actionLabel: 'Review result' }
  return { word: 'Not started', tone: 'quiet', action: 'start', actionLabel: 'Start task' }
}

/** The key of the task a quick signature created: the one in THIS request's own state, never read from a list. */
export function quickKey(state: { id: string; key: string | null } | null, request: { id: string } | null): string | null {
  return state && request && state.id === request.id && state.key ? state.key : null
}
