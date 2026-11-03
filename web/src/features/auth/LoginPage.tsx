import { useSearchParams } from 'react-router-dom'

import { auth0LoginUrl, auth0SignUpUrl, safeReturnPath } from '../../api/auth'
import { PublicFooter, PublicNav } from '../welcome/PublicNav'

const SIGN_IN_ERRORS: Record<string, string> = {
  auth0: "Sign-in didn't complete. Try again.",
  not_allowed: "This address isn't on the sign-in list.",
  verify_email: 'Check your email for a link to verify your address, then sign in.',
}

/** `/login`: the studio's front door -- one door, Auth0's hosted login, plus the inline
 * states the API's error redirects ask for via `?error=`. */
export function LoginPage() {
  const [params] = useSearchParams()
  const returnTo = safeReturnPath(params.get('return_to') ?? '/')
  const signInError = SIGN_IN_ERRORS[params.get('error') ?? '']

  return (
    <>
      <PublicNav signIn={false} title="Sign in" />
      <main className="route-state route-state--public" id="main-content" tabIndex={-1}>
        <div className="route-state__card sign-in-card">
          <h1>Sign in</h1>
          <p className="sign-in-card__subtext">Sign in to reach your projects, subjects, motions and renders.</p>
          {signInError && <p className="sign-in-card__alert" role="alert">{signInError}</p>}
          {/* A plain anchor, not fetch: `/studio/auth/login` answers with a 302 to Auth0. */}
          <a className="button button--primary button--large sign-in-card__submit" href={auth0LoginUrl(returnTo)}>Sign in</a>
          <p className="sign-in-card__subtext">New here? <a className="policy-link" href={auth0SignUpUrl(returnTo)}>Create an account</a> with Google or an email and password.</p>
        </div>
      </main>
      <PublicFooter />
    </>
  )
}
