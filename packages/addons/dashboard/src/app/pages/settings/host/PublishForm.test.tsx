import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import { PublishForm } from './PublishServer'

const noop = () => {}
const URL = 'http://127.0.0.1:8102'

it('a hub workspace sees where files go by name, no address; the address is behind Details', async () => {
  render(<PublishForm server={URL} share={URL} setServer={noop} setShare={noop} suggested={URL} suggestedFrom="hub" hubName="Anna’s hub" />)
  const box = screen.getByTestId('publish-suggested')
  expect(box).toHaveTextContent('Files go to your hub, Anna’s hub.') // by name, in the main view
  expect(box).not.toHaveTextContent(URL)
  expect(screen.queryByLabelText('Publish server address')).toBeNull()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Details' }))
  expect(await screen.findByTestId('info-drawer-content')).toHaveTextContent('Files and links go through your hub: Anna’s hub.')
  expect(screen.getByRole('button', { name: 'Change' })).toBeInTheDocument()
})

it('a local install keeps its own sentence, behind Details', async () => {
  render(<PublishForm server={URL} share={URL} setServer={noop} setShare={noop} suggested={URL} suggestedFrom="computer" />)
  expect(screen.getByTestId('publish-suggested')).toHaveTextContent('Files go to this computer.')
  expect(screen.getByTestId('publish-suggested')).not.toHaveTextContent(URL)
  await userEvent.setup().click(screen.getByRole('button', { name: 'Details' }))
  expect(await screen.findByTestId('info-drawer-content')).toHaveTextContent('Publish server on this computer')
})

it('Change inside Details opens the address field', async () => {
  render(<PublishForm server={URL} share={URL} setServer={noop} setShare={noop} suggested={URL} suggestedFrom="hub" />)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Details' }))
  await user.click(await screen.findByRole('button', { name: 'Change' }))
  expect(await screen.findByLabelText('Publish server address')).toBeInTheDocument()
})
