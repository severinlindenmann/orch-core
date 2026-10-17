export type SignMethod = 'terminal' | 'click' | 'phone'
export interface SignDefault { method: SignMethod; fallback?: SignMethod; is_default: boolean; allowed: SignMethod[] }
export interface SignContent {
  headline: string
  /** B11: the second line under the headline ("and its text (3 sections)"), core's words. */
  subline?: string
  ticket?: { key: string; title: string; uid_tail: string; key_note: string }
  sections?: { id: string; title: string; text: string }[]
  acceptance?: { id: string; text: string; checked?: boolean }[]
  tasks?: { n: number; id: string; title: string; verify?: string[] }[]
  fields_changed?: { field: string; before: string | null; after: string | null; before_note?: string }[]
  note?: string | string[]
  notes?: string[]
}
export const methodLabel: Record<SignMethod, string> = { terminal: 'Sign in Terminal', click: 'One click', phone: 'Approve on your phone' }
