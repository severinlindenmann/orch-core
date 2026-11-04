import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { demoRunList } from '@/mocks/addons/focus/demo'
import { DelegateSheet } from '../DelegateSheet'

/** The old Tasks popover is replaced by the agent tree; "Delegate a task" stays as a button of its own. */
export function LiveDelegate({ ws, ticket }: { ws: string; ticket: string }) {
  const [open, setOpen] = useState(false)
  return <>
    <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setOpen(true)}>Delegate a task</Button>
    {open && <DelegateSheet ws={ws} ticket={ticket} parent={demoRunList().runs[0]} used={2} max={4} onClose={() => setOpen(false)} />}
  </>
}
