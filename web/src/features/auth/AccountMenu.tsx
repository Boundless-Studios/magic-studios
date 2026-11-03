import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { authNavigation } from '../../api/auth'
import { studioApi } from '../../api/client'
import { hostedService } from '../../hostedService'
import { useStudioSession } from './sessionContext'

/** The account control in the header, right of the GPU status button: who is signed in,
 * a way to their API keys, and sign-out. */
export function AccountMenu() {
  const session = useStudioSession()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const label = session?.email ?? 'Account'

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  async function signOut() {
    setError(null)
    try {
      // A full navigation, not a route change: `logout_url` is on the Auth0 tenant.
      const { logout_url: logoutUrl } = await studioApi.signOut()
      authNavigation.redirect(logoutUrl ?? '/login')
    } catch {
      setError('Sign-out failed. Try again.')
    }
  }

  return (
    <div className="account-menu" ref={containerRef}>
      <button aria-expanded={open} aria-haspopup="menu" className="button account-menu__trigger" onClick={() => setOpen(!open)} type="button">
        {label}
      </button>
      {open && (
        <div className="account-menu__panel" role="menu">
          <button onClick={() => { setOpen(false); navigate('/account/api-keys') }} role="menuitem" type="button">API keys</button>
          <button onClick={() => { setOpen(false); navigate('/account/machines') }} role="menuitem" type="button">Your machines</button>
          {session?.admin && hostedService && <button onClick={() => { setOpen(false); navigate('/account/access-requests') }} role="menuitem" type="button">Past access requests</button>}
          <button onClick={() => { setOpen(false); navigate('/guide') }} role="menuitem" type="button">Guide</button>
          <button onClick={() => { setOpen(false); navigate('/about') }} role="menuitem" type="button">About</button>
          <button onClick={() => { setOpen(false); navigate('/storage-policy') }} role="menuitem" type="button">Storage policy</button>
          <button onClick={() => { setOpen(false); navigate('/privacy') }} role="menuitem" type="button">Privacy</button>
          <button onClick={() => { setOpen(false); navigate('/terms') }} role="menuitem" type="button">Terms</button>
          <button onClick={signOut} role="menuitem" type="button">Sign out</button>
          <button onClick={() => { setOpen(false); navigate('/account/delete') }} role="menuitem" type="button">Delete account</button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}
