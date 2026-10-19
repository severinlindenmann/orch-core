export type SignMethod = 'terminal' | 'click' | 'phone'
export interface SignDefault { method: SignMethod; fallback?: SignMethod; is_default: boolean; allowed: SignMethod[] }
export interface SignContent {
  headline: string
  /** U3: the headline a person reads (no host-key / signed-id qualifier; those are in `ticket`, under Technical details). */
  title?: string
  /** V4: a plain lead worked out by core from the SIGNED values only; the exact values always follow in full. */
  summary?: { title: string; lines: string[] }
  /** B11: the second line under the headline ("and its text (3 sections)"), core's words. */
  subline?: string
  ticket?: { key: string; title: string; uid_tail: string; key_note: string; /** This review creates the ticket (U3): its signed id is then only a technical detail. */ new?: boolean }
  sections?: { id: string; title: string; text: string }[]
  acceptance?: { id: string; text: string; checked?: boolean }[]
  tasks?: { n: number; id: string; title: string; verify?: string[] }[]
  fields_title?: string
  fields_changed?: { field: string; before: string | null; after: string | null; before_note?: string }[]
  note?: string | string[]
  notes?: string[]
}
export const methodLabel: Record<SignMethod, string> = { terminal: 'Sign in Terminal', click: 'One click', phone: 'Approve on your phone' }
