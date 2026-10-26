export type SignMethod = 'terminal' | 'click' | 'phone'
export interface SignDefault { method: SignMethod; fallback?: SignMethod; is_default: boolean; allowed: SignMethod[] }
export interface SignContent {
  headline: string
  /** U3: the headline a person reads (no host-key / signed-id qualifier; those are in `ticket`, under Technical details). */
  title?: string
  /** V4: a plain lead worked out by core from the SIGNED values only; the exact values always follow in full. */
  summary?: { title: string; lines: string[]; /** One sentence, only when there is a real risk (amber). */ risk?: string }
  /** B11: the second line under the headline ("and its text (3 sections)"), core's words. */
  subline?: string
  ticket?: { key: string; title: string; uid_tail: string; key_note: string; /** This review creates the ticket (U3): its signed id is then only a technical detail. */ new?: boolean }
  sections?: { id: string; title: string; text: string }[]
  /** R3 part B: pasted screenshots the review adds; shown from the browser's own bytes when the sha256 matches. */
  images?: { name: string; sha256: string; bytes: number; alt: string }[]
  acceptance?: { id: string; text: string; checked?: boolean; manual?: string }[]
  tasks?: { n: number; id: string; title: string; verify?: string[] }[]
  fields_title?: string
  fields_changed?: { field: string; before: string | null; after: string | null; before_note?: string }[]
  note?: string | string[]
  cancels?: { gate: string; people: string[]; complete: boolean }[]
  notes?: string[]
}
export const methodLabel: Record<SignMethod, string> = { terminal: 'Sign in a terminal', click: 'One click', phone: 'Approve on your phone' }
