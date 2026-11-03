import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DownloadChecksums } from './DownloadChecksums'

describe('DownloadChecksums', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows each file with the SHA-256 the server reports', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      files: [{ name: 'magic_studios_mcp-0.2.2-py3-none-any.whl', sha256: 'a'.repeat(64) }],
    }))))
    render(<DownloadChecksums />)
    expect(await screen.findByText('magic_studios_mcp-0.2.2-py3-none-any.whl')).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith('/studio/downloads')
  })

  it('points to the list when the server cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline') }))
    render(<DownloadChecksums />)
    expect(await screen.findByRole('link', { name: '/studio/downloads' })).toHaveAttribute('href', '/studio/downloads')
  })
})
