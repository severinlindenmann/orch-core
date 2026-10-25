import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import AddonForm from '@/addon-ui/AddonForm'
import { changedLines, formSnapshot } from './AddonSettings'

const SECRET = 'sk-live-9f8e7d6c5b4a'

/** A credential is named by the schema, not by the input type: a text input and a textarea must never be echoed. */
it('a secret in a text input or a textarea never appears in the "Approve and save" lines', async () => {
  const user = userEvent.setup()
  const { container } = render(
    <div>
      <AddonForm
        schema={{
          type: 'object',
          properties: {
            name: { type: 'string', title: 'Display name' },
            token: { type: 'string', title: 'API token', writeOnly: true },
            pem: { type: 'string', title: 'Private key', format: 'password' },
            cert: { type: 'string', title: 'Certificate', writeOnly: true },
          },
        }}
        uiSchema={{ cert: { 'ui:widget': 'textarea' } }}
      />
    </div>,
  )
  const root = container.firstElementChild as HTMLElement
  const before = formSnapshot(root)
  await user.type(screen.getByLabelText(/Display name/), 'Acme')
  await user.type(screen.getByLabelText(/API token/), SECRET)
  await user.type(screen.getByLabelText(/Private key/), SECRET)
  await user.type(screen.getByLabelText(/Certificate/), SECRET)
  expect(screen.getByLabelText(/API token/)).toHaveAttribute('data-secret', 'true')
  expect(screen.getByLabelText(/Certificate/).tagName).toBe('TEXTAREA')
  expect(screen.getByLabelText(/Certificate/)).toHaveAttribute('data-secret', 'true')
  const lines = changedLines(root, before)
  expect(lines.join('\n')).not.toContain(SECRET)
  expect(lines).toEqual(expect.arrayContaining(['Display name: empty → Acme', 'API token: new value', 'Private key: new value', 'Certificate: new value']))
})

/** A mark on a container (or a select / radio) covers everything under it; a non-secret sibling still shows old -> new. */
const S1 = 'sk-array-1111'
const S2 = 'sk-object-2222'
const S3 = 'sk-deep-3333'
it('a secret container, a deep nested secret, a secret select and a secret radio are never echoed', async () => {
  const user = userEvent.setup()
  const { container } = render(
    <div>
      <AddonForm
        schema={{
          type: 'object',
          properties: {
            name: { type: 'string', title: 'Display name' },
            keys: { type: 'array', title: 'Keys', writeOnly: true, items: { type: 'string', title: 'Key' } },
            creds: { type: 'object', title: 'Creds', writeOnly: true, properties: { tok: { type: 'string', title: 'Tok' } } },
            outer: {
              type: 'object',
              title: 'Outer',
              properties: { mid: { type: 'object', title: 'Mid', format: 'secret', properties: { deep: { type: 'string', title: 'Deep' } } } },
            },
            tier: { type: 'string', title: 'Tier', enum: ['alpha-secret', 'beta-secret'], writeOnly: true },
            mode: { type: 'string', title: 'Mode', enum: ['red-secret', 'blue-secret'], writeOnly: true },
          },
        }}
        uiSchema={{ mode: { 'ui:widget': 'radio' } }}
        formData={{ keys: [''] }}
      />
    </div>,
  )
  const root = container.firstElementChild as HTMLElement
  const before = formSnapshot(root)
  await user.type(screen.getByLabelText(/Display name/), 'Acme')
  await user.type(screen.getByLabelText(/^Key/), S1)
  await user.type(screen.getByLabelText(/Tok/), S2)
  await user.type(screen.getByLabelText(/Deep/), S3)
  await user.selectOptions(screen.getByLabelText(/Tier/), 'beta-secret')
  await user.click(screen.getByLabelText('blue-secret'))
  const text = changedLines(root, before).join('\n')
  for (const s of [S1, S2, S3, 'beta-secret', 'blue-secret']) expect(text).not.toContain(s)
  expect(text).toContain('Display name: empty → Acme')
  expect(text).toContain('Tok: new value')
  expect(text).toContain('Deep: new value')
  expect(text).toContain('Tier: new value')
})
