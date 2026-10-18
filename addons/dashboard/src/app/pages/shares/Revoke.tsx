// Revoke a share: the link stops working at once. One idem per opening, kept for every retry.
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { newIdem, shareError, sharesApi, type Share } from '@/api/shares'
import { refreshShares } from './ShareFlow'

export function RevokeShare({ ws, share, onClose }: { ws: string; share: Share; onClose: () => void }) {
  const qc = useQueryClient()
  const [idem] = useState(newIdem)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const go = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await sharesApi.revoke(ws, share.id, idem)
      await refreshShares(qc, ws)
      onClose()
    } catch (e) {
      setError(shareError(e))
      void refreshShares(qc, ws)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md gap-3 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Stop sharing these files?</DialogTitle>
          <DialogDescription>
            The link for {share.ticket ? <Inline>{share.ticket}</Inline> : 'this share'} stops working at once. Anyone who has it can no longer open the files. This cannot be undone; to share again, make a new link.
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p role="alert" className="text-[13px] text-danger">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => void go()} disabled={busy}>
            {busy ? 'Stopping…' : 'Stop sharing'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
