import { useId, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { ParsedOptions } from './optionsSchema'
import { addonName, FromAddon, wordsAndId } from './SignConfirm'

const MAX = 120
const cap = (v: string) => (v.length > MAX ? `${v.slice(0, MAX)}…` : v)

type Options = ParsedOptions

/**
 * Core's small dialog for an action the manifest marks `confirm: 'options'`: one native select per field, the
 * package's labels as plain capped text, Cancel as the default focus. Not a signature; the chosen values are posted
 * with the action and the host validates them. Core builds the title and the structure; the package's words (label,
 * note, the row's name) and every arg that will be sent, the chosen values included, sit in the "From the addon" region.
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
        <FromAddon addon={addon} addonTitle={addonTitle} label={label} text={options.note} subject={subject} args={{ ...args, ...values }} />
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            onConfirm(values)
          }}
        >
          {options.fields.map((f) => (
            <div key={f.key} className="grid gap-1">
              <label htmlFor={`${id}-${f.key}`} className="text-[13px] text-text-muted">
                {cap(f.label)}
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
                    {cap(c.label)}
                  </option>
                ))}
              </select>
            </div>
          ))}
          <DialogFooter className="gap-2">
            <Button ref={cancel} type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">{cap(label)}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** "Choose: Share once (share_once) · Publish (publish)": core's words only. */
export const chooseTitle = (action: string, addonTitle: string, addon: string) => `Choose: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
