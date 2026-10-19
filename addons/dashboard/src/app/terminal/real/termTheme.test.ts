import { describe, expect, it } from 'vitest'
import { TERM_THEME, termTheme } from './termTheme'

const ansiKeys = [
  'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
  'brightBlack', 'brightRed', 'brightGreen', 'brightYellow',
  'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite',
] as const

function validColour(value: unknown): boolean {
  if (typeof value !== 'string') return false
  if (/^#[\da-f]{6}$/i.test(value)) return true
  const rgba = /^rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*(0(?:\.\d+)?|1(?:\.0+)?)\s*\)$/.exec(value)
  return rgba !== null && rgba.slice(1, 4).every((channel) => Number(channel) <= 255)
}

function luminance(hex: string): number {
  if (!/^#[\da-f]{6}$/i.test(hex)) throw new Error(`Expected opaque hex: ${hex}`)
  const linear = [1, 3, 5].map((offset) => {
    const srgb = parseInt(hex.slice(offset, offset + 2), 16) / 255
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
  })
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
}

function contrast(foreground: string, background: string): number {
  const a = luminance(foreground)
  const b = luminance(background)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

describe('terminal theme', () => {
  it.each([
    ...ansiKeys, 'background', 'foreground', 'cursor', 'cursorAccent',
    'selectionBackground', 'selectionInactiveBackground',
  ] as const)('defines a valid colour for %s', (key) => {
    expect(validColour(TERM_THEME[key])).toBe(true)
  })

  it.each(ansiKeys.filter((key) => key !== 'black'))('%s has at least 4.5:1 contrast', (key) => {
    expect(TERM_THEME.background).toBe('#0e1014')
    expect(contrast(TERM_THEME[key]!, TERM_THEME.background!)).toBeGreaterThanOrEqual(4.5)
  })

  it('falls back to the constant when no CSS background variable is set', () => {
    const el = document.createElement('div')
    expect(getComputedStyle(el).getPropertyValue('--bg')).toBe('')
    expect(termTheme(el)).toBe(TERM_THEME)
  })

  it('overrides only the background from the host CSS variable', () => {
    const el = document.createElement('div')
    el.style.setProperty('--bg', '  #15181d  ')
    expect(termTheme(el)).toEqual({ ...TERM_THEME, background: '#15181d' })
    expect(TERM_THEME.background).toBe('#0e1014')
  })
})
