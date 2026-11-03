import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { authNavigation } from '../../api/auth'
import { AccountMenu } from './AccountMenu'
import { StudioSessionContext } from './sessionContext'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => { server.resetHandlers(); vi.restoreAllMocks() })
afterAll(() => server.close())

const session = { actorId: 'email:someone@example.com', email: 'someone@example.com', displayName: null, signInMethod: 'auth0' as const }

function renderMenu(value: typeof session | null = session) {
  return render(
    <MemoryRouter initialEntries={['/projects/p-1/motions']}>
      <StudioSessionContext.Provider value={value}>
        <Routes>
          <Route path="/projects/:projectId/motions" element={<AccountMenu />} />
          <Route path="/login" element={<p>Sign in</p>} />
          <Route path="/account/api-keys" element={<p>Keys page</p>} />
        </Routes>
      </StudioSessionContext.Provider>
    </MemoryRouter>,
  )
}

describe('Account menu', () => {
  it('shows past access requests to admins only', async () => {
    const user = userEvent.setup()
    renderMenu()
    await user.click(screen.getByRole('button', { name: 'someone@example.com' }))
    expect(screen.queryByRole('menuitem', { name: 'Past access requests' })).not.toBeInTheDocument()

    renderMenu({ ...session, admin: true } as typeof session)
    await user.click(screen.getAllByRole('button', { name: 'someone@example.com' })[1])
    expect(screen.getByRole('menuitem', { name: 'Past access requests' })).toBeInTheDocument()
  })

  it('labels itself with the signed-in email and falls back to "Account"', () => {
    renderMenu()
    expect(screen.getByRole('button', { name: 'someone@example.com' })).toBeInTheDocument()
    renderMenu(null)
    expect(screen.getByRole('button', { name: 'Account' })).toBeInTheDocument()
  })

  it('signs out through the API and follows the Auth0 logout URL', async () => {
    const user = userEvent.setup()
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    server.use(http.post('/studio/auth/sign-out', () => HttpResponse.json({ logout_url: 'https://tenant.auth0.com/v2/logout?client_id=abc&returnTo=http%3A%2F%2Flocalhost%2Flogin' })))
    renderMenu()

    await user.click(screen.getByRole('button', { name: 'someone@example.com' }))
    await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))

    await vi.waitFor(() => expect(redirect).toHaveBeenCalledWith('https://tenant.auth0.com/v2/logout?client_id=abc&returnTo=http%3A%2F%2Flocalhost%2Flogin'))
  })

  it('falls back to the sign-in page when no logout URL comes back', async () => {
    const user = userEvent.setup()
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    server.use(http.post('/studio/auth/sign-out', () => HttpResponse.json({})))
    renderMenu()

    await user.click(screen.getByRole('button', { name: 'someone@example.com' }))
    await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))

    await vi.waitFor(() => expect(redirect).toHaveBeenCalledWith('/login'))
  })

  it('opens the API keys page from the menu', async () => {
    const user = userEvent.setup()
    renderMenu()
    await user.click(screen.getByRole('button', { name: 'someone@example.com' }))
    await user.click(screen.getByRole('menuitem', { name: 'API keys' }))
    expect(await screen.findByText('Keys page')).toBeInTheDocument()
  })
})
