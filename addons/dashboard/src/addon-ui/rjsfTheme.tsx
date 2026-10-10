// Dark shadcn-styled RJSF theme: inputs, selects, checkboxes, submit button. Everything else uses RJSF defaults
// for structure (objects, arrays) but with quiet templates.
import type { ThemeProps } from '@rjsf/core'
import type { BaseInputTemplateProps, FieldTemplateProps, ObjectFieldTemplateProps, SubmitButtonProps, WidgetProps } from '@rjsf/utils'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

function BaseInputTemplate(props: BaseInputTemplateProps) {
  const { id, value, type, placeholder, disabled, readonly, required, autofocus, onChange, onBlur, onFocus } = props
  return (
    <Input
      id={id}
      name={props.name || id}
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

function TextareaWidget({ id, name, value, disabled, readonly, placeholder, onChange }: WidgetProps) {
  return (
    <Textarea
      id={id}
      name={name || id}
      value={value ?? ''}
      placeholder={placeholder}
      disabled={disabled || readonly}
      className="bg-surface-2 text-[13px]"
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

/** Native <select>: keyboard and screen-reader friendly, and what forms/tests expect for enums. */
function SelectWidget({ id, name, value, options, disabled, readonly, placeholder, onChange }: WidgetProps) {
  const opts = (options.enumOptions ?? []) as { value: unknown; label: string }[]
  const current = value === undefined || value === null ? '' : String(value)
  return (
    <select
      id={id}
      name={name || id}
      value={current}
      disabled={disabled || readonly}
      className="h-8 w-full rounded-md border border-input bg-surface-2 px-2 text-[13px] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50"
      onChange={(e) => onChange(opts.find((o) => String(o.value) === e.target.value)?.value)}
    >
      {current === '' && <option value="">{placeholder || 'Choose'}</option>}
      {opts.map((o) => (
        <option key={String(o.value)} value={String(o.value)}>
          {o.label}
        </option>
      ))}
    </select>
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

/**
 * `ui:options.layout: 'row'` on the root object: a one-line filter bar (auto-fit grid) with the submit button inline.
 * rjsf draws its SubmitButton outside the root field and hands it only the submit options, so the form also sets
 * `ui:globalOptions.layout: 'row'` (rjsf puts that on `registry.globalUiOptions`, which the SubmitButton can read).
 */
const isRow = (uiSchema?: Record<string, unknown>) => (uiSchema?.['ui:options'] as Record<string, unknown> | undefined)?.layout === 'row'
type SubmitOptions = { submitText?: string; norender?: boolean; props?: { disabled?: boolean } }
const submitOptions = (uiSchema?: Record<string, unknown>) =>
  (uiSchema?.['ui:submitButtonOptions'] ?? (uiSchema?.['ui:options'] as Record<string, unknown> | undefined)?.submitButtonOptions) as SubmitOptions | undefined

function ObjectFieldTemplate({ properties, title, description, uiSchema, fieldPathId, registry }: ObjectFieldTemplateProps) {
  if ((isRow(uiSchema) || registry.globalUiOptions?.layout === 'row') && fieldPathId.path.length === 0) {
    const o = submitOptions(uiSchema)
    return (
      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] items-start gap-x-3 gap-y-2">
        {properties.map((p) => (
          <div key={p.name} className="min-w-0">
            {p.content}
          </div>
        ))}
        {/* Same shape as a field (label row, then the control) so the button lines up with the inputs. */}
        {!o?.norender && (
        <div className="space-y-1.5">
          <Label aria-hidden className="invisible text-[12px]">
            .
          </Label>
          <Button type="submit" size="sm" className="h-8" disabled={o?.props?.disabled}>
            {o?.submitText ?? 'Save'}
          </Button>
        </div>
        )}
      </div>
    )
  }
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

function SubmitButton({ uiSchema, registry }: SubmitButtonProps) {
  if (registry.globalUiOptions?.layout === 'row') return null // the row layout draws it inline
  const o = submitOptions(uiSchema)
  if (o?.norender) return null // the caller draws its own submit button (form={id})
  const label = o?.submitText ?? 'Save'
  return (
    <Button type="submit" size="sm" disabled={o?.props?.disabled}>
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
