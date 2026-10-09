import type { AddonDecision } from '@/api/types'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { useAddons } from './slots'

/**
 * Core's signing prompt for an addon decision (Today's cards and any addon surface that posts a decision action).
 * Every fact comes from the decision core holds (GET addon-decisions), never from the addon node's args: the addon,
 * the title and question, and the label of the chosen option. Only after signing is the action posted with core's
 * `confirmed` flag; the host refuses a decision without it (409 confirm.required).
 */
export function DecisionSignPrompt({ d, option, workspacePrefix, onSign, onClose }: { d: AddonDecision; option: AddonDecision['options'][number]; workspacePrefix: string; onSign: () => void; onClose: () => void }) {
  // The addon by its display name (its manifest says it, so it could say anything) and always its package id.
  const { data: packages } = useAddons()
  const title = packages?.find((p) => p.name === d.addon)?.title
  const addonName = title && title !== d.addon ? `${title} (${d.addon})` : d.addon
  return (
    <SignPrompt
      title={`Decide: ${d.title}`}
      covers={[`The question: ${d.question}`, `Your answer: ${option.label}`, `Requested by the addon ${addonName}${d.ticket ? ` about ${d.ticket}` : ''}`, `In workspace ${workspacePrefix}`]}
      confirmLabel="Send answer"
      onClose={onClose}
      onSign={onSign}
    >
      {d.detail && (
        <section aria-label={`From addon ${d.addon}`} className="rounded-md border border-dashed border-border p-2 text-[13px] text-text-muted">
          <p className="mb-0.5 text-[12px]">From the addon {addonName}</p>
          <p className="break-words text-text">{d.detail}</p>
        </section>
      )}
    </SignPrompt>
  )
}

/** The body core posts for a decision after its prompt: the decision's own id and ticket, the option key, `confirmed`. */
export const decisionBody = (d: AddonDecision, option: string) => ({ id: d.id, option, ...(d.ticket ? { ticket: d.ticket } : {}), confirmed: true })
