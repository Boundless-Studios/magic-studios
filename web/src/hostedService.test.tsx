import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

/** A self-hosted build: no `VITE_HOSTED_SERVICE`, so nothing links to the hosted
 * service's tip pages or its past-access-request review. The other tests run as the
 * hosted build (vite.config.ts). */
async function selfHosted() {
  vi.stubEnv('VITE_HOSTED_SERVICE', '')
  vi.resetModules()
  const [{ hostedService }, { PublicNav }, { AboutPage }, { TermsOfService }, { AccountMenu }, { StudioSessionContext }] = await Promise.all([
    import('./hostedService'),
    import('./features/welcome/PublicNav'),
    import('./features/welcome/AboutPage'),
    import('./features/legal/TermsOfService'),
    import('./features/auth/AccountMenu'),
    import('./features/auth/sessionContext'),
  ])
  return { hostedService, PublicNav, AboutPage, TermsOfService, AccountMenu, StudioSessionContext }
}

function renderAt(element: ReactElement) {
  return render(<MemoryRouter>{element}</MemoryRouter>)
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('a self-hosted build', () => {
  it('is not the hosted service', async () => {
    expect((await selfHosted()).hostedService).toBe(false)
  })

  it('has no Support link in the top bar', async () => {
    const { PublicNav } = await selfHosted()
    renderAt(<PublicNav />)
    const nav = screen.getByRole('navigation', { name: 'Magic Studios' })
    expect(within(nav).queryByRole('link', { name: 'Support' })).not.toBeInTheDocument()
    expect(within(nav).getByRole('link', { name: 'About' })).toBeInTheDocument()
  })

  it('has no tips section on the about page', async () => {
    const { AboutPage } = await selfHosted()
    renderAt(<AboutPage />)
    expect(screen.queryByRole('heading', { name: 'Tips' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'leave a tip' })).not.toBeInTheDocument()
  })

  it('has no tips section in the terms', async () => {
    const { TermsOfService } = await selfHosted()
    renderAt(<TermsOfService />)
    expect(screen.queryByRole('heading', { name: 'Tips' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'leave a tip' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Fees' })).toBeInTheDocument()
  })

  it('offers an admin no review of past access requests', async () => {
    const { AccountMenu, StudioSessionContext } = await selfHosted()
    const admin = { actorId: 'email:admin@example.com', email: 'admin@example.com', displayName: null, signInMethod: 'auth0' as const, admin: true }
    renderAt(<StudioSessionContext.Provider value={admin}><AccountMenu /></StudioSessionContext.Provider>)
    await userEvent.setup().click(screen.getByRole('button', { name: 'admin@example.com' }))
    expect(screen.queryByRole('menuitem', { name: 'Past access requests' })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
  })
})
