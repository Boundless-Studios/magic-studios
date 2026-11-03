import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { LoginPage } from './LoginPage'

function renderLogin(path = '/login') {
  return render(<MemoryRouter initialEntries={[path]}><LoginPage /></MemoryRouter>)
}

describe('Sign-in page', () => {
  it('offers one door: a same-origin Auth0 start link carrying the return path', () => {
    renderLogin('/login?return_to=%2Fprojects%2Fp-1%2Fmotions')
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2Fprojects%2Fp-1%2Fmotions')
  })

  it('refuses to send an off-origin return path to the login route', () => {
    renderLogin('/login?return_to=https%3A%2F%2Fevil.invalid%2Fsteal')
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F')
  })

  it('shows the sign-in failure state', () => {
    renderLogin('/login?error=auth0')
    expect(screen.getByRole('alert')).toHaveTextContent("Sign-in didn't complete. Try again.")
  })

  it('shows the not-allowed state without offering a way to ask for access', () => {
    renderLogin('/login?error=not_allowed')
    expect(screen.getByRole('alert')).toHaveTextContent("This address isn't on the sign-in list.")
    expect(screen.queryByRole('link', { name: /request access/i })).not.toBeInTheDocument()
  })

  it('offers Auth0 sign-up beside sign-in, carrying the same return path', () => {
    renderLogin('/login?return_to=%2Fprojects')
    expect(screen.getByRole('link', { name: 'Create an account' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2Fprojects&signup=true')
  })

  it('asks a new password account to verify its email', () => {
    renderLogin('/login?error=verify_email')
    expect(screen.getByRole('alert')).toHaveTextContent('Check your email for a link to verify your address, then sign in.')
  })

  it('ignores an unknown error code rather than rendering an empty alert', () => {
    renderLogin('/login?error=expired')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument()
  })
})
