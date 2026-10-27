import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DatePicker } from './DatePicker'
import { TimePicker } from './TimePicker'
import { DateTimePicker } from './DateTimePicker'
import { nextMonday, parseDateStr, toDateStr } from './dates'

const now = new Date(2026, 9, 27, 10, 0) // Tue 27 Oct 2026

describe('dates', () => {
  it('next Monday is strictly after today', () => {
    expect(toDateStr(nextMonday(now))).toBe('2026-11-02')
    expect(toDateStr(nextMonday(new Date(2026, 10, 2)))).toBe('2026-11-09')
  })
  it('rejects impossible dates', () => expect(parseDateStr('2026-02-30')).toBeNull())
})

describe('DatePicker', () => {
  it('picks Tomorrow and Next Monday with one click', async () => {
    const onChange = vi.fn()
    render(<DatePicker value="" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Tomorrow' }))
    expect(onChange).toHaveBeenCalledWith('2026-10-28')
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Next Monday' }))
    expect(onChange).toHaveBeenLastCalledWith('2026-11-02')
  })
  it('disables days before min', async () => {
    render(<DatePicker value="" onChange={() => {}} min="2026-10-28" now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    expect((await screen.findByRole('button', { name: 'Today' })).hasAttribute('disabled')).toBe(true)
  })
})

describe('TimePicker', () => {
  it('chips and stepper', async () => {
    const onChange = vi.fn()
    render(<TimePicker value="09:00" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: /Time/ }))
    await userEvent.click(await screen.findByRole('button', { name: '15 minutes later' }))
    expect(onChange).toHaveBeenCalledWith('09:15')
    await userEvent.click(screen.getByRole('button', { name: 'Evening' }))
    expect(onChange).toHaveBeenLastCalledWith('18:00')
  })
})

describe('DateTimePicker', () => {
  it('keeps the time when the day changes', async () => {
    const onChange = vi.fn()
    render(<DateTimePicker value="2026-10-27T15:30" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Day/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Tomorrow' }))
    expect(onChange).toHaveBeenCalledWith('2026-10-28T15:30')
  })
})
