// Adding a quick task as a person: a signed `new` (type chore), then a second signed change that sets the label quick
// (host contract 12.9, 9 item 12). Both go through the workspace's ONE signing path (HandoffDialog: click where it is on,
// the terminal where it is off); this file adds no path of its own. The UI says "two signatures".
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import type { FollowUpAction, HandoffAction } from '@/api/handoff'
import { QUICK_LABEL, TWO_SIGNATURES } from '@/api/quickRecords'
import { CompleteDetails } from '@/app/pages/new-ticket/CompleteDetails'
import { ownKey } from '@/app/pages/new-ticket'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** The most a title may hold: one line (the host's own limit is wider; a quick task is one line). */
export const MAX_QUICK_TITLE = 120

export const oneLine = (v: string): string | null => {
  const t = v.trim()
  return t && !/[\r\n]/.test(t) && [...t].length <= MAX_QUICK_TITLE ? t : null
}

const LABEL_STEP: FollowUpAction[] = [{ action: 'details', key: '', labels: [QUICK_LABEL] }]

export function AddQuick({ ws }: { ws: string }) {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [first, setFirst] = useState<HandoffAction | null>(null)
  const [second, setSecond] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const title = oneLine(text)
  const tooLong = [...text.trim()].length > MAX_QUICK_TITLE
  const done = () => {
    setFirst(null)
    setSecond(null)
    setText('')
    void qc.invalidateQueries({ queryKey: ['quick', ws] })
    input.current?.focus()
  }
  return (
    <section aria-label="Add a quick task" className="space-y-2 rounded-lg border border-border bg-surface p-3">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (title) setFirst({ action: 'new', title, type: 'chore' })
        }}
      >
        <div className="min-w-0 flex-1 basis-64 space-y-1.5">
          <Label htmlFor="quick-title">Quick task (one line)</Label>
          <Input id="quick-title" ref={input} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={tooLong ? true : undefined} placeholder="Fix the typo in CONTRIBUTING" autoComplete="off" />
        </div>
        <Button type="submit" disabled={!title || !!first || !!second}>
          Add
        </Button>
      </form>
      <p data-testid="quick-two-signatures" className="text-[12px] text-text-muted">
        {TWO_SIGNATURES}
      </p>
      {tooLong && (
        <p role="alert" className="text-[12px] text-danger">
          A quick task is one line of at most {MAX_QUICK_TITLE} characters.
        </p>
      )}
      {first && (
        <HandoffDialog
          title="New quick task (1 of 2)"
          ws={ws}
          action={first}
          autoStart
          onClose={() => setFirst(null)}
          onSettled={(phase, state, request) => {
            if (phase !== 'done') return
            const key = ownKey(state, request)
            setFirst(null)
            void qc.invalidateQueries({ queryKey: ['quick', ws] })
            if (key) setSecond(key)
            else {
              toast.success('The task is created. Its quick label could not be followed here; set it from the ticket.')
              done()
            }
          }}
        />
      )}
      {second && <CompleteDetails ws={ws} ticketKey={second} steps={LABEL_STEP} title="Label as quick (2 of 2)" onFinish={done} />}
    </section>
  )
}
