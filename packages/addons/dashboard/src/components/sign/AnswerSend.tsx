import type { QuestionStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
export function AnswerSend({ canSend, fallback, type = 'button',
}: {
  ticket: string
  question: QuestionStatus
  option?: string | null
  text?: string
  /** An answer is chosen and the ticket is loaded. */
  canSend: boolean
  /** The existing dialog (two steps), for every case the strip does not serve. */
  fallback: () => void
  /** The question text of the signer's View, when it is shown (it replaces the card's text); null otherwise. */
  onViewQuestion?: (text: string | null) => void
  /** True while the signature is on its way or done: the owner of the answer form locks the option and the note. */
  onBusy?: (busy: boolean) => void
  type?: 'button' | 'submit'
}) {
  return <div className="flex justify-end"><Button type={type} size="sm" disabled={!canSend} onClick={type === 'submit' ? undefined : fallback}>Send answer</Button></div>
}
/** What the live region says for a phase of the strip (nothing for a refusal: its own message is on screen). */
export function liveText(phase: string, armed: boolean): string {
  if (phase === 'signing' || phase === 'settling') return 'Sending your answer.'
  if (phase === 'done') return 'Answer sent.'
  if (phase !== 'ready') return ''
  return armed ? 'Send answer is ready.' : 'Review loaded. Send answer turns on in a moment.'
}
