import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'

export type DropdownOption = { value: string; label: string; disabled?: boolean }

// Radix Select does not accept an empty value, so "" (for example "None") travels as this sentinel.
const EMPTY = '__empty__'

/** The one dropdown for every choice list (replaces the browser's native select). */
export function Dropdown({ value, onChange, options, id, ariaLabel, className, disabled, placeholder, testId }: {
  value: string
  onChange: (value: string) => void
  options: DropdownOption[]
  id?: string
  ariaLabel?: string
  className?: string
  disabled?: boolean
  placeholder?: string
  testId?: string
}) {
  return (
    <Select value={value === '' ? EMPTY : value} onValueChange={(v) => onChange(v === EMPTY ? '' : v)} disabled={disabled}>
      <SelectTrigger id={id} aria-label={ariaLabel} data-testid={testId} className={cn('h-8 w-full text-[13px]', className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value === '' ? EMPTY : o.value} disabled={o.disabled}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
