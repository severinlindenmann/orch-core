import { describe, expect, it, vi } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DatePicker } from './DatePicker'
import { TimePicker } from './TimePicker'
import { DateTimePicker } from './DateTimePicker'
import { nextMonday, parseDateStr, toDateStr } from './dates'

const now = new Date(2026, 9, 27, 10, 0) // Tue 27 Oct 2026
const monthName = (y: number, m: number) => new Date(y, m, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
const focused = () => (document.activeElement as HTMLElement | null)?.dataset.day

describe('dates', () => {
  it('next Monday is strictly after today', () => {
    expect(toDateStr(nextMonday(now))).toBe('2026-11-02')
    expect(toDateStr(nextMonday(new Date(2026, 10, 2)))).toBe('2026-11-09')
  })
  it('next Monday uses the local day, also late on a Sunday evening', () => {
    expect(toDateStr(nextMonday(new Date(2026, 10, 1, 23, 30)))).toBe('2026-11-02')
    expect(toDateStr(nextMonday(new Date(2026, 10, 2, 0, 15)))).toBe('2026-11-09')
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
  it('clearing the date calls onChange with an empty value', async () => {
    const onChange = vi.fn()
    render(<DatePicker value="2026-10-28" onChange={onChange} now={now} allowClear />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'No date' }))
    expect(onChange).toHaveBeenCalledWith('')
  })
  it('the month is a real grid: rows of gridcells with the day buttons inside', async () => {
    render(<DatePicker value="2026-10-27" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    const grid = await screen.findByRole('grid', { name: 'Days' })
    const rows = within(grid).getAllByRole('row')
    expect(rows).toHaveLength(6) // weekday header + 5 weeks of October 2026 (Monday first)
    expect(within(rows[0]).getAllByRole('columnheader')).toHaveLength(7)
    for (const row of rows.slice(1)) expect(within(row).getAllByRole('gridcell')).toHaveLength(7)
    const day = grid.querySelector('[data-day="2026-10-27"]')!
    expect(day.closest('[role="gridcell"]')).toBeTruthy()
    // Roving tabindex: only the selected day is in the tab order.
    expect(Array.from(grid.querySelectorAll('button')).filter((b) => b.tabIndex === 0).map((b) => b.dataset.day)).toEqual(['2026-10-27'])
  })
  it('arrow keys, Home/End and PageUp/PageDown move the focused day', async () => {
    const onChange = vi.fn()
    render(<DatePicker value="2026-10-27" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    const grid = await screen.findByRole('grid', { name: 'Days' })
    act(() => grid.querySelector<HTMLButtonElement>('[data-day="2026-10-27"]')!.focus())
    await userEvent.keyboard('{ArrowRight}')
    expect(focused()).toBe('2026-10-28')
    await userEvent.keyboard('{ArrowLeft}{ArrowLeft}')
    expect(focused()).toBe('2026-10-26')
    await userEvent.keyboard('{ArrowDown}')
    expect(focused()).toBe('2026-11-02')
    expect(screen.getByTestId('date-picker-month').textContent).toBe(monthName(2026, 10))
    await userEvent.keyboard('{ArrowUp}')
    expect(focused()).toBe('2026-10-26')
    await userEvent.keyboard('{End}')
    expect(focused()).toBe('2026-11-01')
    await userEvent.keyboard('{Home}')
    expect(focused()).toBe('2026-10-26')
    await userEvent.keyboard('{PageDown}')
    expect(focused()).toBe('2026-11-26')
    await userEvent.keyboard('{PageUp}{PageUp}')
    expect(focused()).toBe('2026-09-26')
    await userEvent.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledWith('2026-09-26')
  })
  it('arrow keys stop at min', async () => {
    render(<DatePicker value="2026-10-28" min="2026-10-27" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    const grid = await screen.findByRole('grid', { name: 'Days' })
    act(() => grid.querySelector<HTMLButtonElement>('[data-day="2026-10-28"]')!.focus())
    await userEvent.keyboard('{ArrowUp}')
    expect(focused()).toBe('2026-10-27')
    await userEvent.keyboard('{ArrowLeft}')
    expect(focused()).toBe('2026-10-27')
  })
  it('opening shows the month of the value, or today when empty', async () => {
    const { rerender } = render(<DatePicker value="2026-10-27" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Next month' }))
    await userEvent.click(screen.getByRole('button', { name: 'Next month' }))
    expect(screen.getByTestId('date-picker-month').textContent).toBe(monthName(2026, 11))
    await userEvent.keyboard('{Escape}')
    rerender(<DatePicker value="2027-03-05" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    expect((await screen.findByTestId('date-picker-month')).textContent).toBe(monthName(2027, 2))
    await userEvent.keyboard('{Escape}')
    rerender(<DatePicker value="" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Date/ }))
    expect((await screen.findByTestId('date-picker-month')).textContent).toBe(monthName(2026, 9))
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
  it('the stepper never wraps past midnight and is disabled at the limits', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<TimePicker value="23:45" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: /Time/ }))
    expect((await screen.findByRole('button', { name: '15 minutes later' })).hasAttribute('disabled')).toBe(true)
    rerender(<TimePicker value="23:40" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: '15 minutes later' }))
    expect(onChange).toHaveBeenLastCalledWith('23:45')
    rerender(<TimePicker value="00:00" onChange={onChange} />)
    expect(screen.getByRole('button', { name: '15 minutes earlier' }).hasAttribute('disabled')).toBe(true)
    rerender(<TimePicker value="00:10" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: '15 minutes earlier' }))
    expect(onChange).toHaveBeenLastCalledWith('00:00')
  })
  it('minTime disables earlier chips and clamps the stepper', async () => {
    const onChange = vi.fn()
    render(<TimePicker value="10:15" minTime="10:07" onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: /Time/ }))
    expect((await screen.findByRole('button', { name: 'Morning' })).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Noon' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: '15 minutes earlier' }).hasAttribute('disabled')).toBe(true)
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
  it("moving to min's day with an earlier time takes min's time, rounded up to 15 minutes", async () => {
    const onChange = vi.fn()
    render(<DateTimePicker value="2026-10-28T09:00" min="2026-10-27T10:07" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Day/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Today' }))
    expect(onChange).toHaveBeenCalledWith('2026-10-27T10:15')
  })
  it("keeps a later time on min's day", async () => {
    const onChange = vi.fn()
    render(<DateTimePicker value="2026-10-28T15:30" min="2026-10-27T10:07" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Day/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Today' }))
    expect(onChange).toHaveBeenCalledWith('2026-10-27T15:30')
  })
  it("default time: min's time on min's day, else 09:00", async () => {
    const onChange = vi.fn()
    render(<DateTimePicker value="" min="2026-10-27T10:07" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /Day/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Today' }))
    expect(onChange).toHaveBeenLastCalledWith('2026-10-27T10:15')
    await userEvent.click(screen.getByRole('button', { name: /Day/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Tomorrow' }))
    expect(onChange).toHaveBeenLastCalledWith('2026-10-28T09:00')
  })
  it("on min's day, earlier times are not offered", async () => {
    const onChange = vi.fn()
    render(<DateTimePicker value="2026-10-27T10:15" min="2026-10-27T10:07" onChange={onChange} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /^Time/ }))
    expect((await screen.findByRole('button', { name: 'Morning' })).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: '15 minutes earlier' }).hasAttribute('disabled')).toBe(true)
  })
  it('another day allows any time', async () => {
    render(<DateTimePicker value="2026-10-28T10:15" min="2026-10-27T10:07" onChange={() => {}} now={now} />)
    await userEvent.click(screen.getByRole('button', { name: /^Time/ }))
    expect((await screen.findByRole('button', { name: 'Morning' })).hasAttribute('disabled')).toBe(false)
  })
})
