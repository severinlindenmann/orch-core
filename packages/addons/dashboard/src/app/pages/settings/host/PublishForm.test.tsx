import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PublishForm } from './PublishServer'

const noop = () => {}
const URL = 'http://127.0.0.1:8102'

it('a hub workspace sees one sentence naming the hub, no address field', () => {
  render(<PublishForm server={URL} share={URL} setServer={noop} setShare={noop} suggested={URL} suggestedFrom="hub" hubName="Anna’s hub" />)
  expect(screen.getByTestId('publish-suggested')).toHaveTextContent('Files and links go through your hub: Anna’s hub.')
  expect(screen.queryByLabelText('Publish server address')).toBeNull()
})

it('a local install keeps its own sentence', () => {
  render(<PublishForm server={URL} share={URL} setServer={noop} setShare={noop} suggested={URL} suggestedFrom="computer" />)
  expect(screen.getByTestId('publish-suggested')).toHaveTextContent('Publish server on this computer')
})
