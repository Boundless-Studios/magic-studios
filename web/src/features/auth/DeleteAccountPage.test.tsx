import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { authNavigation } from '../../api/auth'
import { DeleteAccountPage } from './DeleteAccountPage'
import { StudioSessionContext } from './sessionContext'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => { server.resetHandlers(); vi.restoreAllMocks() })
afterAll(() => server.close())

const session = { actorId: 'email:someone@example.com', email: 'someone@example.com', displayName: null, signInMethod: 'auth0' as const }

function renderPage() {
  return render(<MemoryRouter><StudioSessionContext.Provider value={session}><DeleteAccountPage /></StudioSessionContext.Provider></MemoryRouter>)
}

describe('Delete account', () => {
  it('deletes only after the email is typed, then signs out', async () => {
    let body: unknown = null
    server.use(http.delete('/studio/account', async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ logout_url: 'https://tenant.example/v2/logout' })
    }))
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    renderPage()

    const confirm = screen.getByRole('button', { name: 'Delete my account permanently' })
    expect(confirm).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type your email/), 'Someone@Example.com')
    await userEvent.click(confirm)

    await vi.waitFor(() => expect(redirect).toHaveBeenCalledWith('https://tenant.example/v2/logout'))
    expect(body).toEqual({ confirm_email: 'Someone@Example.com' })
  })

  it('reports a refusal and stays on the page', async () => {
    server.use(http.delete('/studio/account', () => HttpResponse.json(
      { detail: { code: 'deletion_not_confirmed', message: 'type your account email to delete it' } }, { status: 422 })))
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    renderPage()

    await userEvent.type(screen.getByLabelText(/Type your email/), 'someone@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Delete my account permanently' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('type your account email to delete it')
    expect(redirect).not.toHaveBeenCalled()
  })
})
