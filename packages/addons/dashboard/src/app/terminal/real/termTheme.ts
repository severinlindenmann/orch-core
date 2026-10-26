import type { ITheme } from '@xterm/xterm'

export const TERM_FONT_FAMILY = "'Geist Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"

export const TERM_LOOK = {
  fontSize: 13,
  lineHeight: 1.25,
  letterSpacing: 0,
  cursorStyle: 'bar',
  cursorWidth: 2,
  cursorInactiveStyle: 'outline',
} as const

export const TERM_PADDING = '10px 12px'

// Token-related hues with varied lightness to help separate colours when hue
// perception is reduced. Bright black remains readable for comments/dim text.
export const TERM_THEME: ITheme = {
  background: '#0e1014',
  foreground: '#d7dae0',
  cursor: '#2fc6a3',
  cursorAccent: '#0e1014',
  selectionBackground: 'rgba(47, 198, 163, 0.24)',
  selectionInactiveBackground: 'rgba(47, 198, 163, 0.12)',
  black: '#3b4252',
  red: '#ed737b',
  green: '#73da91',
  yellow: '#edc45b',
  blue: '#699fe8',
  magenta: '#ba9aea',
  cyan: '#38bea9',
  white: '#c4c9d1',
  brightBlack: '#8b949e',
  brightRed: '#ff9399',
  brightGreen: '#98edaf',
  brightYellow: '#ffe094',
  brightBlue: '#94beff',
  brightMagenta: '#d6baff',
  brightCyan: '#79dfcd',
  brightWhite: '#e8eaed',
}

export function termTheme(el: HTMLElement): ITheme {
  const background = getComputedStyle(el).getPropertyValue('--bg').trim()
  return background ? { ...TERM_THEME, background } : TERM_THEME
}
