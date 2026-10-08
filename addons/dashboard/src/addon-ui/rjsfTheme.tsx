// Dark shadcn-styled RJSF theme: inputs, selects, checkboxes, submit button. Everything else uses RJSF defaults
// for structure (objects, arrays) but with quiet templates.
import type { ThemeProps } from '@rjsf/core'
import type { BaseInputTemplateProps, FieldTemplateProps, ObjectFieldTemplateProps, SubmitButtonProps, WidgetProps } from '@rjsf/utils'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

function BaseInputTemplate(props: BaseInputTemplateProps) {
  const { id, value, type, placeholder, disabled, readonly, required, autofocus, onChange, onBlur, onFocus } = props
  return (
    <Input
      id={id}
      type={type === 'integer' ? 'number' : (type ?? 'text')}
      value={value ?? ''}
      placeholder={placeholder}
      disabled={disabled || readonly}
      required={required}
      autoFocus={autofocus}
      className="h-8 bg-surface-2 text-[13px]"
      onChange={(e) => onChange(e.target.value === '' ? props.options.emptyValue : e.target.value)}
      onBlur={(e) => onBlur(id, e.target.value)}
      onFocus={(e) => onFocus(id, e.target.value)}
    />
  )
}

function TextareaWidget({ id, value, disabled, readonly, placeholder, onChange }: WidgetProps) {
  return (
    <Textarea
      id={id}
      value={value ?? ''}
      placeholder={placeholder}
      disabled={disabled || readonly}
      className="bg-surface-2 text-[13px]"
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

function SelectWidget({ id, value, options, disabled, readonly, placeholder, onChange }: WidgetProps) {
  const opts = (options.enumOptions ?? []) as { value: unknown; label: string }[]
  const current = value === undefined || value === null ? undefined : String(value)
  return (
    <Select
      value={current}
      disabled={disabled || readonly}
      onValueChange={(v) => onChange(opts.find((o) => String(o.value) === v)?.value)}
    >
      <SelectTrigger id={id} size="sm" className="w-full bg-surface-2 text-[13px]">
        <SelectValue placeholder={placeholder || 'Choose'} />
      </SelectTrigger>
      <SelectContent>
        {opts.map((o) => (
          <SelectItem key={String(o.value)} value={String(o.value)}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function CheckboxWidget({ id, value, label, disabled, readonly, onChange }: WidgetProps) {
  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} checked={!!value} disabled={disabled || readonly} onCheckedChange={(c) => onChange(c === true)} />
      <Label htmlFor={id} className="text-[13px] font-normal">
        {label}
      </Label>
    </div>
  )
}

function FieldTemplate({ id, label, children, errors, help, description, hidden, displayLabel, required }: FieldTemplateProps) {
  if (hidden) return <div className="hidden">{children}</div>
  return (
    <div className="space-y-1.5">
      {displayLabel && label && (
        <Label htmlFor={id} className="text-[12px] text-text-muted">
          {label}
          {required ? ' *' : ''}
        </Label>
      )}
      {description}
      {children}
      {errors}
      {help}
    </div>
  )
}

function ObjectFieldTemplate({ properties, title, description }: ObjectFieldTemplateProps) {
  return (
    <div className="space-y-3">
      {title && <div className="text-[13px] font-semibold">{title}</div>}
      {description}
      {properties.map((p) => (
        <div key={p.name}>{p.content}</div>
      ))}
    </div>
  )
}

function SubmitButton({ uiSchema }: SubmitButtonProps) {
  const label = (uiSchema?.['ui:submitButtonOptions'] as { submitText?: string } | undefined)?.submitText ?? 'Save'
  return (
    <Button type="submit" size="sm">
      {label}
    </Button>
  )
}

export const darkTheme: ThemeProps = {
  widgets: { TextareaWidget, SelectWidget, CheckboxWidget },
  templates: {
    BaseInputTemplate,
    FieldTemplate,
    ObjectFieldTemplate,
    ButtonTemplates: { SubmitButton } as never,
    FieldErrorTemplate: ({ errors }: { errors?: string[] }) => (
      <ul className="text-[12px] text-danger">{errors?.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul>
    ),
    ErrorListTemplate: () => null,
  },
}
