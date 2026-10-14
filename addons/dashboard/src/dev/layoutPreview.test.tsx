import { cleanup, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { resetLayoutPreview } from './layoutPreview'

const original = { ...api }
afterEach(() => {
  cleanup()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
})

it('preview swaps are reversible, including after a second installation', async () => {
  const first = renderApp('/__layout/records-ok')
  await screen.findByTestId('records-export-warning')
  expect(api.getRecords).not.toBe(original.getRecords)
  first.unmount()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
  renderApp('/__layout/records-first')
  await screen.findByTestId('records-not-configured')
  expect(api.getRecords).not.toBe(original.getRecords)
})
