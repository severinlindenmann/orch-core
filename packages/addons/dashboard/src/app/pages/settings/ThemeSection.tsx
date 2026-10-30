import { Section } from '../ticket/shared'
import { setChoice, useThemeChoice, type ThemeChoice } from '@/lib/theme'
import { cn } from '@/lib/utils'

const CHOICES: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

/** Settings > Theme: kept in this browser only, applied at once. */
export function ThemeSection() {
  const choice = useThemeChoice()
  return (
    <Section title="Theme">
      <div role="radiogroup" aria-label="Theme" className="inline-flex gap-1 rounded-md border border-border bg-surface-2 p-1">
        {CHOICES.map((c) => (
          <button
            key={c.value}
            type="button"
            role="radio"
            aria-checked={choice === c.value}
            data-testid={'theme-' + c.value}
            onClick={() => setChoice(c.value)}
            className={cn(
              'min-h-8 rounded px-3 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring',
              choice === c.value ? 'bg-nav-selected font-medium text-text shadow-[inset_0_0_0_1px_var(--nav-selected-border)]' : 'text-text-muted hover:text-text',
            )}
          >
            {c.label}
          </button>
        ))}
      </div>
      <p className="mt-2 text-[12px] text-text-muted">System follows your computer. This choice is kept in this browser only.</p>
    </Section>
  )
}
