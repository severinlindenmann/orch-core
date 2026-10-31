import { Button } from '@/components/ui/button'

const REFUSED: Record<string, string> = {
  unknown_user: "Someone you share with can't be reached through the hub.",
  other_person: "Someone you share with can't be reached through the hub.",
  too_large: 'This ticket is too large to share through the hub.',
}
export function ShareDelivery({ state, onSend }: { state?: string; onSend: () => void }) {
  if (state === undefined) return null
  const text = state === 'sent' ? 'Shared with the hub'
    : state === 'not_sent' ? 'Not sent yet'
    : state?.startsWith('refused:') ? (Object.hasOwn(REFUSED, state.slice(8)) ? REFUSED[state.slice(8)] : 'The hub did not take this share.')
    : 'Waiting for the hub to receive it'
  return <span data-testid="shared-chip" className="inline-flex flex-wrap items-center gap-2 text-xs text-text-muted">
    {text}{state === 'not_sent' && <Button size="sm" variant="ghost" onClick={onSend}>Send now</Button>}
  </span>
}
