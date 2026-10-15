import { AddonBadge, useSlot } from '@/addon-ui'
import { SlidersHorizontal } from 'lucide-react'
import { STATUSES } from '@/api/types'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { STATUS_LABEL, type BoardDisplay } from './lib'

function SwitchRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  const id = `display-${label.replace(/\s+/g, '-').toLowerCase()}`
  return (
    <div className="flex items-center justify-between gap-3">
      <Label htmlFor={id} className="text-[12px] font-normal text-text">
        {label}
      </Label>
      <Switch id={id} aria-label={label} checked={checked} onCheckedChange={onChange} />
    </div>
  )
}

/** What the cards show and which columns are collapsed to a rail. */
export function DisplayPopover({ display, onChange }: { display: BoardDisplay; onChange: (patch: Partial<BoardDisplay>) => void }) {
  const fields = useSlot('board.card_field')
  const sources = fields.filter((c, i) => fields.findIndex(f => f.addon === c.addon) === i)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-2.5 text-[12px] text-text-muted outline-none hover:bg-accent hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
        >
          <SlidersHorizontal className="size-3.5" aria-hidden /> Display
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="flex w-64 flex-col gap-3 p-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Density</span>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={display.density}
            onValueChange={(v) => v && onChange({ density: v as BoardDisplay['density'] })}
            aria-label="Density"
            className="w-full"
          >
            <ToggleGroupItem value="comfortable" className="h-8 flex-1 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
              Comfortable
            </ToggleGroupItem>
            <ToggleGroupItem value="compact" className="h-8 flex-1 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
              Compact
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Group</span>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={display.group}
            onValueChange={(v) => v && onChange({ group: v as BoardDisplay['group'] })}
            aria-label="Group by"
            className="w-full"
          >
            <ToggleGroupItem value="epic" className="h-8 flex-1 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
              Epic
            </ToggleGroupItem>
            <ToggleGroupItem value="none" className="h-8 flex-1 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
              None
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-2">
          <SwitchRow label="Show labels" checked={display.labels} onChange={(labels) => onChange({ labels })} />
          <SwitchRow label="Show estimate" checked={display.estimate} onChange={(estimate) => onChange({ estimate })} />
          <SwitchRow label="Show progress" checked={display.progress} onChange={(progress) => onChange({ progress })} />
        </div>
        {sources.map(c => <p key={c.addon} className="flex items-center gap-1 text-xs text-text-muted"><span>{c.title} from {c.addonTitle}</span><AddonBadge name={c.addon} title={c.addonTitle} /></p>)}
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1 text-[11px] font-medium uppercase tracking-wide text-text-faint">Collapsed columns</legend>
          {STATUSES.map((s) => (
            <div key={s} className="flex items-center gap-2">
              <Checkbox
                id={`collapse-${s}`}
                checked={display.collapsed.includes(s)}
                onCheckedChange={(on) => onChange({ collapsed: on ? [...display.collapsed, s] : display.collapsed.filter((x) => x !== s) })}
              />
              <Label htmlFor={`collapse-${s}`} className="text-[12px] font-normal text-text">
                {STATUS_LABEL[s]}
              </Label>
            </div>
          ))}
        </fieldset>
      </PopoverContent>
    </Popover>
  )
}
