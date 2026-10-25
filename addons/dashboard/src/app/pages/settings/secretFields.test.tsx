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
