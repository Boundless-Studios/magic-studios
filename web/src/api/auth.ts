export type SignInMethod = 'auth0' | 'google' | 'iap'

export interface StudioSession {
  actorId: string
  email: string | null
  displayName: string | null
  signInMethod: SignInMethod | null
  /** A verified studio admin: sees past access requests. */
  admin?: boolean
}

const SIGN_IN_METHODS: readonly string[] = ['auth0', 'google', 'iap']

export async function readStudioSession(response: Response): Promise<StudioSession | null> {
  if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) return null
  const payload = await response.json() as { actor_id?: unknown; email?: unknown; display_name?: unknown; sign_in_method?: unknown; admin?: unknown }
  if (typeof payload.actor_id !== 'string' || !payload.actor_id) return null
  return {
    actorId: payload.actor_id,
    email: typeof payload.email === 'string' && payload.email ? payload.email : null,
    displayName: typeof payload.display_name === 'string' && payload.display_name ? payload.display_name : null,
    signInMethod: typeof payload.sign_in_method === 'string' && SIGN_IN_METHODS.includes(payload.sign_in_method) ? payload.sign_in_method as SignInMethod : null,
    admin: payload.admin === true,
  }
}

/** Only a same-origin *path* may be handed back as `return_to`; anything absolute or
 * protocol-relative would turn our own redirect into an open redirect. */
export function safeReturnPath(returnPath: string): string {
  return returnPath.startsWith('/') && !returnPath.startsWith('//') && !returnPath.includes('\\') ? returnPath : '/'
}

export function currentReturnPath(): string {
  return safeReturnPath(`${window.location.pathname}${window.location.search}${window.location.hash}`)
}

export function loginUrl(returnPath: string): string {
  const safePath = safeReturnPath(returnPath)
  // A signed-out visit to the front page meets the public welcome page, not a bare sign-in card.
  return safePath === '/' ? '/welcome' : `/login?return_to=${encodeURIComponent(safePath)}`
}

/** A browser navigation, never a fetch: the server answers with a 302 to Auth0's hosted login. */
export function auth0LoginUrl(returnPath: string): string {
  return `/studio/auth/login?return_to=${encodeURIComponent(safeReturnPath(returnPath))}`
}

/** The same round trip, opening Auth0's hosted page on its sign-up form. */
export function auth0SignUpUrl(returnPath: string): string {
  return `${auth0LoginUrl(returnPath)}&signup=true`
}

/** Indirection so tests can observe the 401 redirect: jsdom cannot navigate. */
export const authNavigation = {
  redirect(url: string) { window.location.replace(url) },
}

/** Every 401 -- the session check or any studioApi call -- lands the user on the sign-in
 * page carrying the path they wanted, which the API's 303 returns them to. */
export function redirectToLogin(): void {
  if (window.location.pathname === '/login') return
  authNavigation.redirect(loginUrl(currentReturnPath()))
}

export async function fetchStudioSession(): Promise<StudioSession | null> {
  try {
    return readStudioSession(await fetch('/studio/auth/session', { credentials: 'same-origin', redirect: 'manual' }))
  } catch {
    return null
  }
}
