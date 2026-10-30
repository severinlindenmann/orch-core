import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { applyChoice, setChoice, storedChoice, THEME_KEY } from './theme'

const root = document.documentElement
const media = (light: boolean) => vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('light') ? light : !light, addEventListener() {}, removeEventListener() {} }))

beforeEach(() => {
  localStorage.clear()
  root.removeAttribute('data-theme')
  root.className = ''
  root.style.colorScheme = ''
})
afterEach(() => vi.unstubAllGlobals())

describe('theme choice (#421)', () => {
  it('index.html (also the setup wizard page) loads the boot script before the app, and names both schemes', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const html = readFileSync(join(__dirname, '../../index.html'), 'utf8')
    const boot = html.indexOf('src="/theme-boot.js"')
    expect(boot).toBeGreaterThan(-1)
    expect(boot).toBeLessThan(html.indexOf('type="module"'))
    expect(html).toContain('name="color-scheme" content="light dark"')
  })
  it('a stored Light sets data-theme before anything renders', () => {
    localStorage.setItem(THEME_KEY, 'light')
    media(false)
    applyChoice()
    expect(root.getAttribute('data-theme')).toBe('light')
    expect(root.classList.contains('dark')).toBe(false)
    expect(root.style.colorScheme).toBe('light')
  })
  it('a stored Dark wins over a light browser', () => {
    localStorage.setItem(THEME_KEY, 'dark')
    media(true)
    applyChoice()
    expect(root.getAttribute('data-theme')).toBe('dark')
    expect(root.classList.contains('dark')).toBe(true)
  })
  it('System (nothing stored) leaves data-theme off and follows the browser', () => {
    media(true)
    applyChoice()
    expect(root.hasAttribute('data-theme')).toBe(false)
    expect(root.style.colorScheme).toBe('light')
    media(false)
    applyChoice()
    expect(root.classList.contains('dark')).toBe(true)
  })
  it('setChoice stores and applies at once; System clears the store', () => {
    media(false)
    setChoice('light')
    expect(localStorage.getItem(THEME_KEY)).toBe('light')
    expect(root.getAttribute('data-theme')).toBe('light')
    setChoice('system')
    expect(localStorage.getItem(THEME_KEY)).toBeNull()
    expect(root.hasAttribute('data-theme')).toBe(false)
  })
  it('blocked storage does not throw and falls back to System', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    media(false)
    expect(storedChoice()).toBe('system')
    expect(() => applyChoice()).not.toThrow()
    vi.restoreAllMocks()
  })
  it('the boot script (public/theme-boot.js) sets the same things before the app loads', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const src = readFileSync(join(__dirname, '../../public/theme-boot.js'), 'utf8')
    localStorage.setItem(THEME_KEY, 'light')
    media(false)
    new Function(src)()
    expect(root.getAttribute('data-theme')).toBe('light')
    expect(root.style.colorScheme).toBe('light')
    localStorage.setItem(THEME_KEY, 'dark')
    new Function(src)()
    expect(root.getAttribute('data-theme')).toBe('dark')
    expect(root.classList.contains('dark')).toBe(true)
  })
})
