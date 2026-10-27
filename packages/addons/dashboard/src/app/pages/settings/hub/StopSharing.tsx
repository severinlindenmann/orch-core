import { Flow } from './shared'
export function StopSharing({ onClose }: { ws: string; ticket: string; onClose: () => void }) {
  return <Flow title="Stop sharing this ticket" onClose={onClose}><p>One click is not ready for this action.</p></Flow>
}
