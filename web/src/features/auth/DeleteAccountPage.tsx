import { useState } from 'react'
import { Link } from 'react-router-dom'

import { authNavigation } from '../../api/auth'
import { studioApi } from '../../api/client'
import { useStudioSession } from './sessionContext'

/** Permanent account deletion. The person types their email; the server removes every
 * project, movie and scene flight with their files, saved provider keys, API keys,
 * attached machines and sessions, then signs them out. */
export function DeleteAccountPage() {
  const session = useStudioSession()
  // Without an email (a local gateway identity) the account is confirmed by its actor id.
  const email = session?.email ?? session?.actorId ?? ''
  const [typed, setTyped] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const confirmed = email !== '' && typed.trim().toLowerCase() === email.toLowerCase()

  async function remove() {
    setDeleting(true)
    setError(null)
    try {
      const { logout_url: logoutUrl } = await studioApi.deleteAccount(typed.trim())
      authNavigation.redirect(logoutUrl ?? '/login')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Your account could not be deleted. Try again.')
      setDeleting(false)
    }
  }

  return <main className="route-state"><div className="route-state__card delete-account">
    <p className="eyebrow">Account</p>
    <h1>Delete your account</h1>
    <p>This permanently deletes, and cannot be undone:</p>
    <ul>
      <li>every project, movie and scene flight, with all their images, takes, releases and exports</li>
      <li>your saved provider keys (such as Higgsfield) and your API keys</li>
      <li>the machines you attached, which can no longer connect</li>
      <li>your sign-in sessions and account record</li>
    </ul>
    <p>Packages you already delivered to an outside target stay where you sent them. Signing in again later starts a new, empty account.</p>
    <label>Type your email ({email || 'signed-in email'}) to confirm<input autoComplete="off" value={typed} onChange={(event) => setTyped(event.target.value)} /></label>
    <div className="delete-item__actions">
      <button className="button delete-item__confirm" type="button" disabled={!confirmed || deleting} onClick={remove}>{deleting ? 'Deleting…' : 'Delete my account permanently'}</button>
      <Link className="button" to="/projects">Cancel</Link>
    </div>
    {error && <p role="alert">{error}</p>}
  </div></main>
}
