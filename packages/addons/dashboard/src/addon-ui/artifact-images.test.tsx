import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ArtifactImagesContext } from './artifactImagesContext'
import { SafeMarkdown } from './SafeMarkdown'

const NAME = 'paste-20261019-101500-ab12cd34.png'
const URL = 'data:image/png;base64,iVBORw0KGgo='

describe('pictures in markdown', () => {
  it('shows an artifact picture only from bytes the page verified, as an <img> with a data: address', async () => {
    const { container } = render(
      <ArtifactImagesContext.Provider value={{ [NAME]: URL }}>
        <SafeMarkdown text={`Look:\n\n![the broken page](artifact:${NAME})`} />
      </ArtifactImagesContext.Provider>,
    )
    const img = await screen.findByTestId('md-picture')
    expect(img).toHaveAttribute('src', URL)
    expect(img).toHaveAttribute('alt', 'the broken page')
    expect(container.querySelectorAll('img')).toHaveLength(1)
  })

  it('shows a placeholder when no verified bytes exist, and never loads another address', async () => {
    const { container } = render(
      <SafeMarkdown text={`![gone](artifact:${NAME})\n\n![remote](https://tracker.example/pixel.png)\n\n![local](file:///etc/passwd)\n\n![data](data:image/svg+xml,<svg onload=alert(1)>)`} />,
    )
    await screen.findByText('[image: gone]')
    expect(screen.getByText('[image: remote]')).toBeInTheDocument()
    expect(container.querySelector('img, [src]')).toBeNull()
  })

  it('does not let an artifact reference become a link', async () => {
    const { container } = render(<SafeMarkdown text={`[open](artifact:${NAME})`} />)
    await screen.findByText('open')
    expect(container.querySelector('a, [href]')).toBeNull()
  })
})

import { isPersonPaste } from './artifactImages'

describe('who may have a picture drawn inline', () => {
  const ok = { name: NAME, kind: 'screenshot', added_by_kind: 'person' }
  it('only a person-added screenshot with a paste name', () => {
    expect(isPersonPaste(ok)).toBe(true)
    expect(isPersonPaste({ ...ok, added_by_kind: 'agent' })).toBe(false)
    expect(isPersonPaste({ ...ok, added_by_kind: 'host' })).toBe(false)
    expect(isPersonPaste({ ...ok, added_by_kind: null })).toBe(false)
    expect(isPersonPaste({ ...ok, kind: 'other' })).toBe(false)
    expect(isPersonPaste({ ...ok, name: 'shot.png' })).toBe(false)
  })
})
