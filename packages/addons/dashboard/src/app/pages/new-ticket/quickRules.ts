import type { NewTicketRequest, TicketType } from '@/api/types'

/**
 * Quick ticket: one line of text (typed or dictated) becomes a backlog ticket.
 * Pure rules, shared by the overlay and the palette. The type is guessed from words; the first match in this order wins.
 */
const TYPE_WORDS: [TicketType, RegExp][] = [
  ['bug', /\b(bugs?|fix(es|ed|ing)?|broken|breaks?|crash(es|ed|ing)?|fails?|failing|failed|regression)\b/],
  ['spike', /\b(spike|investigate|research|explore|find out|figure out|evaluate)\b/],
  ['chore', /\b(chore|clean ?up|bump|upgrade|update dependenc(y|ies)|rename|refactor|tidy)\b/],
]

/** bug / spike / chore by keyword, else feature. Never an epic (an epic needs a summary and children). */
export function guessType(text: string): TicketType {
  const t = text.toLowerCase()
  return TYPE_WORDS.find(([, re]) => re.test(t))?.[0] ?? 'feature'
}

export const QUICK_TITLE_MAX = 80

/** The first sentence of the first line, without trailing punctuation, cut at a word boundary to 80 characters. */
export function quickTitle(text: string): string {
  const line = text.trim().split(/\r?\n/)[0] ?? ''
  const sentence = (line.split(/(?<=[.!?])\s+/)[0] ?? '').replace(/[.!?,;:\s]+$/, '').trim()
  if (sentence.length <= QUICK_TITLE_MAX) return sentence
  const cut = sentence.slice(0, QUICK_TITLE_MAX - 1)
  const space = cut.lastIndexOf(' ')
  return `${(space > 40 ? cut.slice(0, space) : cut).replace(/[.,;:\s]+$/, '')}…`
}

/** Why the text cannot become a ticket yet, or null: at least two words, and a title of 3 characters or more. */
export function quickProblem(text: string): string | null {
  const words = text.trim().split(/\s+/).filter(Boolean).length
  return words < 2 || quickTitle(text).length < 3 ? 'Write at least two words.' : null
}

/** The request the host gets: a backlog ticket with the text as its requirements. */
export function quickRequest(text: string): NewTicketRequest {
  return {
    type: guessType(text),
    title: quickTitle(text),
    priority: 'medium',
    size: null,
    labels: [],
    parent: null,
    due: null,
    visibility: 'workspace',
    people: { owner: null, assignees: [], reviewers: [] },
    sections: { requirements: text.trim() },
    acceptance: [],
  }
}

/** Sample transcriptions for simulated dictation (no audio is recorded). */
export const SAMPLE_TRANSCRIPTS = [
  'The CSV export is broken for months without entries, it should still download a file with the header row.',
  'Add a filter for my own tickets to the board, so I can hide everything assigned to other people.',
  'Investigate why the nightly import takes twice as long since the last release and write down what we find.',
]
