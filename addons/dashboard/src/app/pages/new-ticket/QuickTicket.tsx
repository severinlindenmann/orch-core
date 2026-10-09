import { Zap } from 'lucide-react'
import { useState, type RefObject } from 'react'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dictate } from './Dictate'
import { guessType, quickProblem, quickTitle } from './quickRules'
import { useQuickCreate } from './useQuickCreate'

/**
 * One line, Enter, done: a backlog ticket whose type is guessed from the words and whose requirements are the text.
 * The line can be typed or dictated (simulated).
 */
export function QuickTicket({
  workspaceId,
  canCreate,
  value,
  onChange,
  onCreated,
  inputRef,
}: {
  workspaceId: string
  canCreate: boolean
  value: string
  onChange: (text: string) => void
  onCreated: (ticket: TicketDocument) => void
  inputRef: RefObject<HTMLInputElement | null>
}) {
  const { create, pending } = useQuickCreate(workspaceId)
  const [error, setError] = useState<string | null>(null)
  const text = value.trim()

  const submit = async () => {
    const problem = quickProblem(value)
    setError(problem)
    if (problem || !canCreate) return
    const ticket = await create(value)
    if (ticket) {
      onChange('')
      onCreated(ticket)
    }
  }

  return (
    <form
      aria-label="Quick ticket"
      className="space-y-1.5"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      {/* While dictating, the recording bar takes a row of its own under the line. */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <Zap aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-text-faint" />
          <Input
            ref={inputRef}
            value={value}
            onChange={(e) => {
              onChange(e.target.value)
              if (error) setError(null)
            }}
            disabled={!canCreate}
            placeholder="Quick ticket: describe it in one line, Enter creates it"
            aria-label="Quick ticket"
            aria-invalid={!!error || undefined}
            aria-describedby={error ? 'qt-error qt-hint' : 'qt-hint'}
            className="h-9 pl-8 text-[14px] md:text-[14px]"
          />
        </div>
        <Dictate
          disabled={!canCreate}
          onTranscript={(t) => {
            onChange(t)
            setError(null)
            inputRef.current?.focus()
          }}
        />
        <Button type="submit" variant="outline" className="h-9" aria-label="Create quick ticket" disabled={!canCreate || pending || !text}>
          Create
        </Button>
      </div>
      {error && (
        <p id="qt-error" className="text-[12px] text-danger">
          {error}
        </p>
      )}
      <p id="qt-hint" className="text-[12px] text-text-faint">
        {text && !quickProblem(value) ? (
          <>
            Enter creates a <span className="text-text-muted">{guessType(value)}</span> in Backlog: “{quickTitle(value)}”. The whole text becomes its requirements.
          </>
        ) : (
          <>The type is guessed from words like “bug”, “fix” or “investigate”; the text becomes the requirements. Dictation is simulated — no audio leaves your browser.</>
        )}
      </p>
    </form>
  )
}
