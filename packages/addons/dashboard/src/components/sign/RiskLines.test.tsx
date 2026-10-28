import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RiskLines } from './ClickSignPanel'

afterEach(() => cleanup())

describe('RiskLines', () => {
  it('scrolls the warning into view when it appears, so Sign is never stuck behind the fold', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const box = vi.fn()
    render(<RiskLines lines={['Anything running as you could sign.']} box={box} />)
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' })
    expect(box).toHaveBeenCalled()
  })
  it('draws nothing and scrolls nothing without lines', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    render(<RiskLines lines={[]} />)
    expect(scroll).not.toHaveBeenCalled()
  })
})
