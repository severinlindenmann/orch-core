// Which body sections each ticket type shows (spec §4). Pure data: the UI and the mock both import it.
import type { BodySections, TicketType } from './types'

export type SectionNeed = 'required' | 'optional' | 'absent'
export type SectionName = keyof BodySections

export const SECTION_ORDER: SectionName[] = ['summary', 'context', 'requirements', 'out_of_scope', 'plan', 'decisions', 'verification', 'current_state']

export const SECTION_LABEL: Record<SectionName, string> = {
  summary: 'Summary',
  context: 'Context',
  requirements: 'Requirements',
  out_of_scope: 'Out of scope',
  plan: 'Plan',
  decisions: 'Decisions',
  verification: 'Verification',
  current_state: 'Current state',
}

/** Spikes ask questions and report findings instead. */
export const SECTION_LABEL_BY_TYPE: Partial<Record<TicketType, Partial<Record<SectionName, string>>>> = {
  spike: { requirements: 'Questions to answer', verification: 'Findings' },
}

export const sectionLabel = (type: TicketType, section: SectionName): string => SECTION_LABEL_BY_TYPE[type]?.[section] ?? SECTION_LABEL[section]

const R = 'required'
const O = 'optional'
const A = 'absent'
type Row = [SectionNeed, SectionNeed, SectionNeed, SectionNeed, SectionNeed] // feature, bug, chore, spike, epic
const TABLE: Record<SectionName, Row> = {
  summary: [O, O, O, O, R],
  context: [R, R, O, R, R],
  requirements: [R, R, R, R, R],
  out_of_scope: [R, R, A, A, R],
  plan: [R, R, R, R, A],
  decisions: [R, R, O, R, R],
  verification: [R, R, A, R, A],
  current_state: [R, R, R, R, R],
}
const TYPES: TicketType[] = ['feature', 'bug', 'chore', 'spike', 'epic']

export const SECTIONS_BY_TYPE = Object.fromEntries(
  TYPES.map((t, i) => [t, Object.fromEntries(SECTION_ORDER.map((s) => [s, TABLE[s][i]]))]),
) as Record<TicketType, Record<SectionName, SectionNeed>>

/** The only sections that must have text at creation. Which other section a gate needs: see neededBefore. */
export const requiredAtCreation = (type: TicketType): SectionName[] => (type === 'epic' ? ['summary', 'requirements'] : ['requirements'])

/**
 * Which approval needs the section to have text (core `model.gates._REQUIRED`, the rule behind "Add Out of scope and one
 * acceptance check first"). Context and Out of scope are needed before the REQUIREMENTS gate, the plan and decisions
 * before the PLAN gate. A section no gate asks for is optional, whatever the table above says about the spec.
 */
const GATE_NEEDS: Record<'requirements' | 'plan', Record<TicketType, SectionName[]>> = {
  requirements: {
    feature: ['context', 'requirements', 'out_of_scope'],
    bug: ['context', 'requirements', 'out_of_scope'],
    chore: ['requirements'],
    spike: ['context', 'requirements'],
    epic: ['summary', 'context', 'requirements', 'out_of_scope'],
  },
  plan: { feature: ['plan', 'decisions'], bug: ['plan', 'decisions'], chore: ['plan'], spike: ['plan', 'decisions'], epic: ['decisions'] },
}
export const neededBefore = (type: TicketType, section: SectionName): 'requirements' | 'plan' | null =>
  GATE_NEEDS.requirements[type].includes(section) ? 'requirements' : GATE_NEEDS.plan[type].includes(section) ? 'plan' : null
