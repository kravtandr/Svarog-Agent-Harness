import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

// От корня web/: в jsdom-окружении import.meta.url не файловый URL.
const css = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8')

describe('токены', () => {
  it('совпадают со спеком', () => {
    const expected: Record<string, string> = {
      '--bg': '#1a1917',
      '--surface': '#211f1d',
      '--raised': '#292724',
      '--line': '#322f2b',
      '--line-soft': '#262421',
      '--text': '#eae5dc',
      '--muted': '#a29b90',
      '--faint': '#6e6862',
      '--ember': '#d2622c',
      '--ok': '#6e9b72',
      '--bad': '#c4635c',
      '--git': '#7e93b8',
    }
    for (const [name, value] of Object.entries(expected)) {
      expect(css).toContain(`${name}: ${value};`)
    }
  })

  it('не содержит второго акцентного оранжевого', () => {
    const oranges = css.match(/#[dD][0-9a-fA-F]{5}/g) ?? []
    expect(oranges).toEqual(['#d2622c'])
  })
})
