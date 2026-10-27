import type { TermPreview } from '@/api/runs'
import { Button } from '@/components/ui/button'
export const TERMINAL_LINE = 'It runs on this computer as you, so it can read and change your files and keys.'
export function PreviewDialog({ preview, onClose }: { preview: { data: TermPreview | null; error: string } | null; busy: boolean; onClose: () => void }) {
  if (!preview) return null
  return <div role="alert"><p>{preview.error || preview.data?.blocked?.message || 'Opening terminal…'}</p><Button variant="ghost" onClick={onClose}>Dismiss</Button></div>
}
