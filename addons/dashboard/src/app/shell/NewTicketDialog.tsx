import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useShellState } from './ShellUi'

/** Placeholder: the real form arrives with the ticket pages. */
export function NewTicketDialog() {
  const { newTicketOpen, setNewTicketOpen } = useShellState()
  return (
    <Dialog open={newTicketOpen} onOpenChange={setNewTicketOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New ticket</DialogTitle>
          <DialogDescription>The ticket form comes in a later iteration. Until then, use `orch new` in the terminal.</DialogDescription>
        </DialogHeader>
      </DialogContent>
    </Dialog>
  )
}
