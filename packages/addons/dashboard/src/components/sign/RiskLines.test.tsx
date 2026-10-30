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

describe('RiskLines tone (#421)', () => {
  it('is the warning tone by default and the danger tone (red bar, red icon) when asked', () => {
    const { rerender, getByTestId } = render(<RiskLines lines={['Sends data out.']} />)
    const ul = () => getByTestId('sign-risk-lines')
    expect(ul().dataset.tone).toBe('warning')
    expect(ul().className).toContain('border-l-risk-bar')
    rerender(<RiskLines lines={['Sends data out.']} danger />)
    expect(ul().dataset.tone).toBe('danger')
    expect(ul().className).toContain('border-l-risk-danger-bar')
    expect(ul().className).not.toContain('border-l-risk-bar ')
    expect(ul().querySelector('svg')!.getAttribute('class')).toContain('text-danger')
  })
})
