import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ServerPageRedirect } from './ServerPageRedirect'

describe('ServerPageRedirect', () => {
  afterEach(() => vi.unstubAllGlobals())

  it.each([
    ['/docs', '/studio/docs'],
    ['/redoc', '/studio/reference'],
  ])('sends %s to the page the API serves at %s', (path, target) => {
    const replace = vi.fn()
    vi.stubGlobal('location', { ...window.location, replace })
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/docs" element={<ServerPageRedirect to="/studio/docs" />} />
          <Route path="/redoc" element={<ServerPageRedirect to="/studio/reference" />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(replace).toHaveBeenCalledWith(target)
    expect(screen.getByRole('link', { name: 'API reference' })).toHaveAttribute('href', target)
  })
})
