import { useEffect, useRef, type MouseEvent, type RefObject } from 'react'
import { Link, NavLink } from 'react-router-dom'

import { auth0LoginUrl } from '../../api/auth'
import { hostedService } from '../../hostedService'

const PAGE_LINKS = [
  ['/guide', 'Guide'],
  ['/about', 'About'],
] as const

const POLICY_LINKS = [
  ['/storage-policy', 'Storage policy'],
  ['/privacy', 'Privacy'],
  ['/terms', 'Terms'],
] as const

const SITE_NAME = 'Magic Studios'

/** Names the browser tab after the page, and puts the previous name back on the way out,
 * since signed-in pages keep the default. */
function usePageTitle(title?: string) {
  useEffect(() => {
    const previous = document.title
    document.title = title ? `${title} · ${SITE_NAME}` : SITE_NAME
    return () => { document.title = previous }
  }, [title])
}

/** Publishes the pinned bar's height as `--site-header-height`, so in-page jumps land below
 * it however many rows it wraps to. */
function useHeaderHeight(header: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const element = header.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const root = document.documentElement
    const publish = () => root.style.setProperty('--site-header-height', `${element.offsetHeight}px`)
    // Set it now as well: a page that scrolls to a hash on mount (About's #cost) runs its
    // effect right after this one, before the observer's first callback.
    publish()
    const observer = new ResizeObserver(publish)
    observer.observe(element)
    return () => {
      observer.disconnect()
      root.style.removeProperty('--site-header-height')
    }
  }, [header])
}

/** Moves focus past the pinned bar to the page's `<main id="main-content">`. */
function skipToContent(event: MouseEvent<HTMLAnchorElement>) {
  const main = document.getElementById('main-content')
  if (!main) return
  event.preventDefault()
  main.focus()
  // Keep React Router's entry state (its history index); only the fragment changes.
  window.history.replaceState(window.history.state, '', '#main-content')
}

/** The top bar every signed-out page shares, pinned while the page scrolls, so a visitor
 * never loses the way back. It also names the tab (`title`, usually the page's heading)
 * and offers a skip link to the page's main content. The sign-in page leaves out its own
 * link: its card already carries the return path. */
export function PublicNav({ title, signIn = true }: { title?: string; signIn?: boolean }) {
  const header = useRef<HTMLElement>(null)
  usePageTitle(title)
  useHeaderHeight(header)
  return (
    <header className="site-header" ref={header}>
      <a className="skip-link" href="#main-content" onClick={skipToContent}>Skip to content</a>
      <nav className="site-nav" aria-label="Magic Studios">
        <Link className="site-nav__brand" to="/welcome">✦ Magic Studios</Link>
        <span className="site-nav__links">
          {PAGE_LINKS.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}
        </span>
        {/* On a phone these stay beside the brand and the page links take the second row. */}
        <span className="site-nav__actions">
          {hostedService && <NavLink className="site-nav__support" to="/support">Support</NavLink>}
          {signIn && <a className="site-nav__sign-in" href={auth0LoginUrl('/')}>Sign in</a>}
        </span>
      </nav>
    </header>
  )
}

/** The pinned top bar carries the pages, so the footer holds only the policies. */
export function PublicFooter() {
  return (
    <footer className="site-footer">
      {POLICY_LINKS.map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}
    </footer>
  )
}
