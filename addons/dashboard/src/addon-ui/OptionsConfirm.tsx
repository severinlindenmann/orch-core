import { useId, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { ParsedOptions } from './optionsSchema'
import { plain } from '@/components/sign/visible'
import { addonName, argLines, FromAddon, wordsAndId } from './SignConfirm'
import { SentArgs } from './SentArgs'
import { coreNote } from './coreNotes'


type Options = ParsedOptions

/**
 * Core's small dialog for an action the manifest marks `confirm: 'options'`: one native select per field, Cancel as
 * the default focus. Not a signature; the chosen values are posted with the action and core's `confirmed` flag, and the
 * host validates them. Core's words: the title, the "Sends" list (every arg, the chosen values included, live) and the
 * button ("Continue: …"). The package's words (label, note, the row's name, field and choice labels) sit in its labelled
 * regions, in full.
 */
export function OptionsConfirm({
  addon,
  addonTitle,
  action,
  label,
  subject,
  args,
  ticket,
  options,
  onConfirm,
  onClose,
}: {
  addon: string
  addonTitle: string
  action: string
  label: string
  subject?: string
  /** The args the trigger sends besides the choices. */
  args?: Record<string, unknown>
  ticket?: string
  options: Options
  onConfirm: (values: Record<string, string | number>) => void
  onClose: () => void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  const id = useId()
  const [values, setValues] = useState<Record<string, string | number>>(() => Object.fromEntries(options.fields.map((f) => [f.key, f.default])))
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="max-w-md gap-4 border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          cancel.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>{chooseTitle(action, addonTitle, addon)}</DialogTitle>
          <DialogDescription>{ticket ? `About ${ticket}. ` : ''}Choose, then continue: the choices are sent with the action.</DialogDescription>
        </DialogHeader>
        {coreNote(addon, action) && <p className="text-[13px]" data-testid="core-note">{coreNote(addon, action)}</p>}
        <SentArgs lines={argLines({ ...args, ...values })} />
        <FromAddon addon={addon} addonTitle={addonTitle} label={label} text={options.note} subject={subject} />
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            onConfirm(values)
          }}
        >
          {/* The fields and choices are the package's words: inside its labelled region, apart from core's "Sends". */}
          <section aria-label={`From the addon: choices of ${addon}`} className="max-h-[40vh] space-y-3 overflow-auto rounded-md border border-dashed border-border p-2">
            <p className="text-[12px] text-text-muted">The addon's choices ({plain(addonTitle)})</p>
            {options.fields.map((f) => (
              <div key={f.key} className="grid gap-1">
                <label htmlFor={`${id}-${f.key}`} className="text-[13px] text-text-muted">
                  {f.label}
                </label>
                <select
                  id={`${id}-${f.key}`}
                  name={f.key}
                  value={String(values[f.key])}
                  onChange={(e) => {
                    const c = f.choices.find((x) => String(x.value) === e.target.value)
                    if (c) setValues((v) => ({ ...v, [f.key]: c.value }))
                  }}
                  className="h-9 rounded-md border border-border bg-bg px-2 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-brand"
                >
                  {f.choices.map((c) => (
                    <option key={String(c.value)} value={String(c.value)}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </section>
          <DialogFooter className="gap-2">
            <Button ref={cancel} type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            {/* Core's words: this button makes core send the action with its `confirmed` flag. */}
            <Button type="submit">{`Continue: ${wordsAndId(action)}`}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** "Choose: Share once (share_once) · Publish (publish)": core's words only. */
export const chooseTitle = (action: string, addonTitle: string, addon: string) => `Choose: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
