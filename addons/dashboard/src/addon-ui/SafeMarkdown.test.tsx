import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SafeMarkdown } from './SafeMarkdown'

const hostile = [
  '<script>alert(1)</script>',
  '<img src=x onerror="alert(1)">',
  '[x](javascript:alert(1))',
  '[y](data:text/html,<script>alert(1)</script>)',
  '[z](JaVaScRiPt:alert(1))',
  '[e](&#106;avascript:alert(1))',
  '<javascript:alert(1)>',
  '[ref][r]\n\n[r]: javascript:alert(1)',
  '<a href="javascript:alert(1)" onclick="alert(1)">raw</a>',
  '<iframe src="https://evil.example"></iframe>',
  '<svg onload="alert(1)"></svg>',
  '<details open ontoggle="alert(1)">d</details>',
].join('\n\n')

describe('SafeMarkdown', () => {
  it('renders hostile markup inert', () => {
    const { container } = render(<SafeMarkdown text={hostile} />)
    const html = container.innerHTML
    expect(container.querySelector('script, img, iframe, svg, style, form, object, embed')).toBeNull()
    expect(html).not.toMatch(/\son\w+=/i)
    // The autolink <javascript:...> survives as plain text, which is inert; what matters is that no attribute carries a script URL.
    expect(html).not.toMatch(/=\s*"\s*(javascript|data|vbscript):/i)
    expect(container.querySelector('[href], [src], [action], [formaction], [xlink\\:href]')).toBeNull()
    for (const a of container.querySelectorAll('a')) expect(a.getAttribute('href')).toMatch(/^https?:\/\//)
  })
  it('opens external links with noopener noreferrer', () => {
    const { container } = render(<SafeMarkdown text="[ok](https://example.com)" />)
    const a = container.querySelector('a')!
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toContain('noopener')
    expect(a.getAttribute('rel')).toContain('noreferrer')
  })
})
