import { useEffect } from 'react'

/** Sends the browser to a page the API serves. On magicstudios.ai only `/studio/*`
 * reaches the API, so FastAPI's old `/docs` and `/redoc` land in this app instead; a
 * full page load is needed because `/studio/*` is not a client route. */
export function ServerPageRedirect({ to }: { to: string }) {
  useEffect(() => { window.location.replace(to) }, [to])
  return <p className="route-state">Opening the <a href={to}>API reference</a>…</p>
}
