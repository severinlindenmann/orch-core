import type { TermPreview } from '@/api/runs'
import { Button } from '@/components/ui/button'
export const TERMINAL_LINE = 'It runs on this computer as you, so it can read and change your files and keys.'
/** Why New terminal did not open. `onRetry` (absent for a blocked preview) tries again with the same idem. */
export function PreviewDialog({ preview, busy, onClose, onRetry }: { preview: { data: TermPreview | null; error: string } | null; busy: boolean; onClose: () => void; onRetry?: () => void }) {
  if (!preview) return null
  return <div role="alert"><p>{preview.error || preview.data?.blocked?.message || 'Opening terminal…'}</p>{onRetry && <Button variant="ghost" disabled={busy} onClick={onRetry}>Try again</Button>}<Button variant="ghost" onClick={onClose}>Dismiss</Button></div>
}
