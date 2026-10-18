// Sections for add-ons, one per add-on (ids H, I, J ... and steps H1.1, H1.2 ...), appended after F in run2.ts.
// To add one: push a ChecklistSection here. Ids are free text of the form [A-Z]<n>[.<n>] (the server checks that).
// Notes are stored by step id, so adding or reordering steps here never loses a note.
import type { ChecklistSection } from './run2'

export const ADDON_SECTIONS: ChecklistSection[] = []
