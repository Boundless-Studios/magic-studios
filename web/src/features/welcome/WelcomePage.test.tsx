import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LoginPage } from '../auth/LoginPage'
import { SupportPage, SupportThanksPage } from '../support/SupportPage'
import { GuidePage } from '../guide/GuidePage'
import { PrivacyPolicy } from '../legal/PrivacyPolicy'
import { TermsOfService } from '../legal/TermsOfService'
import { StoragePolicy } from '../policies/StoragePolicy'
import { AboutPage } from './AboutPage'
import { ADVANCE_MS } from './ModeShowcase'
import { WelcomePage } from './WelcomePage'

// The welcome demos need the hosted site media, staged at deploy time from a pinned release.
// The modules above load as a deployed build does, with a media base; the last block of this
// file renders a public build without one.
const MEDIA_BASE = '/assets/site-media/1'
vi.hoisted(() => { vi.stubEnv('VITE_SITE_MEDIA_BASE', '/assets/site-media/1') })

function renderPage(page: React.ReactElement) {
  return render(<MemoryRouter>{page}</MemoryRouter>)
}

const PUBLIC_PAGES = [
  ['welcome', <WelcomePage key="welcome" />, 'Magic Studios'],
  ['about page', <AboutPage key="about" />, 'About · Magic Studios'],
  ['guide', <GuidePage key="guide" />, 'Guide · Magic Studios'],
  ['storage policy', <StoragePolicy key="storage" />, 'Storage policy · Magic Studios'],
  ['privacy policy', <PrivacyPolicy key="privacy" />, 'Privacy policy · Magic Studios'],
  ['terms', <TermsOfService key="terms" />, 'Terms of service · Magic Studios'],
  ['sign-in page', <LoginPage key="login" />, 'Sign in · Magic Studios'],
  ['support page', <SupportPage key="support" />, 'Support · Magic Studios'],
  ['tip thank-you page', <SupportThanksPage key="thanks" />, 'Thank you · Magic Studios'],
] as const

describe.each(PUBLIC_PAGES)('the %s', (name, page, title) => {
  it('carries the same top bar and footer as every other public page', () => {
    renderPage(page)
    const links = (region: HTMLElement) => within(region).getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')])
    const site = [['✦ Magic Studios', '/welcome'], ['Guide', '/guide'], ['About', '/about'], ['Support', '/support']]
    // The sign-in page leaves out its own link.
    expect(links(screen.getByRole('navigation', { name: 'Magic Studios' }))).toEqual(name === 'sign-in page' ? site : [...site, ['Sign in', '/studio/auth/login?return_to=%2F']])
    // The top bar stays pinned, so the footer holds only the policies.
    expect(links(screen.getByRole('contentinfo'))).toEqual([['Storage policy', '/storage-policy'], ['Privacy', '/privacy'], ['Terms', '/terms']])
  })

  it('names the browser tab after itself and puts the old name back on the way out', () => {
    document.title = 'Earlier page'
    const { unmount } = renderPage(page)
    expect(document.title).toBe(title)
    unmount()
    expect(document.title).toBe('Earlier page')
  })

  it('offers a skip link that moves focus past the top bar to the main content', async () => {
    const user = userEvent.setup()
    renderPage(page)
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to content' })
    expect(skip).toHaveFocus()
    window.history.replaceState({ idx: 3, key: 'router-entry' }, '')
    await user.keyboard('{Enter}')
    expect(screen.getByRole('main')).toHaveFocus()
    // Only the fragment changes; React Router's entry state survives.
    expect(window.location.hash).toBe('#main-content')
    expect(window.history.state).toEqual({ idx: 3, key: 'router-entry' })
  })
})

describe('Public pages', () => {
  it('publishes the pinned bar height so in-page jumps clear it, and clears it on the way out', () => {
    class ImmediateResizeObserver { constructor(private callback: () => void) {} observe() { this.callback() } disconnect() {} }
    vi.stubGlobal('ResizeObserver', ImmediateResizeObserver)
    const { unmount } = renderPage(<GuidePage />)
    expect(document.documentElement.style.getPropertyValue('--site-header-height')).toMatch(/^\d+px$/)
    unmount()
    expect(document.documentElement.style.getPropertyValue('--site-header-height')).toBe('')
    vi.unstubAllGlobals()
  })

  it('the about page carries the cost section: nothing to buy and no hosted generation', () => {
    renderPage(<AboutPage />)
    const cost = screen.getByRole('region', { name: 'Cost' })
    expect(cost).toHaveTextContent('Magic Studios is free.')
    expect(within(cost).getByRole('heading', { name: 'Your own GPU: free' })).toBeInTheDocument()
    expect(within(cost).getByRole('link', { name: 'leave a tip' })).toHaveAttribute('href', '/support')
    expect(screen.queryByText(/hosted generation|prepaid|checkout/i)).not.toBeInTheDocument()
  })

  it('the about page scrolls to the cost section, and back to the top when the About link drops the hash', async () => {
    const user = userEvent.setup()
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    render(<MemoryRouter initialEntries={['/about#cost']}><AboutPage /></MemoryRouter>)
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    scrollTo.mockClear()
    await user.click(within(screen.getByRole('navigation', { name: 'Magic Studios' })).getByRole('link', { name: 'About' }))
    expect(scrollTo).toHaveBeenCalledWith(0, 0)
    scrollTo.mockRestore()
    Element.prototype.scrollIntoView = original
  })

  it('the welcome page leads to Auth0 sign-up, to Auth0 sign-in and to the guide', () => {
    renderPage(<WelcomePage />)
    expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F&signup=true')
    expect(screen.getByText(/Sign up with Google or an email and password\./)).toBeInTheDocument()
    expect(screen.queryByText(/Invite only/)).not.toBeInTheDocument()
    for (const signIn of screen.getAllByRole('link', { name: 'Sign in' })) {
      expect(signIn).toHaveAttribute('href', '/studio/auth/login?return_to=%2F')
    }
    expect(screen.getByRole('link', { name: 'guide' })).toHaveAttribute('href', '/guide')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('A free AI game asset studio for agents')
    expect(screen.getByRole('link', { name: 'Connect an agent' })).toHaveAttribute('href', '/guide#agents')
    expect(screen.getByRole('heading', { name: 'Your own GPU' })).toBeInTheDocument()
    expect(screen.getByText(/The studio’s shared machines are for a small allowlist and aren’t available on request; everyone else uses their own\./)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Your Higgsfield key' })).toBeInTheDocument()
  })

  it('the welcome page shows the orc walking in eight directions, with pause and background controls', async () => {
    const user = userEvent.setup()
    renderPage(<WelcomePage />)
    const showcase = screen.getByRole('figure', { name: 'Example animation made in Magic Studios' })
    const sprite = within(showcase).getByRole('img', { name: 'Orc walking southeast, 16 frames' })
    expect(sprite).toHaveAttribute('data-playing', 'true')
    expect(sprite.style.getPropertyValue('--sheet')).toBe(`url(${MEDIA_BASE}/welcome/orc-walk-se.webp)`)
    const directions = within(showcase).getByRole('group', { name: 'Direction' })
    expect(within(directions).getAllByRole('button').map((button) => button.getAttribute('aria-label'))).toEqual(
      ['Walk north', 'Walk northeast', 'Walk east', 'Walk southeast', 'Walk south', 'Walk southwest', 'Walk west', 'Walk northwest'],
    )
    expect(within(directions).getByRole('button', { name: 'Walk southeast' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(directions).getByRole('button', { name: 'Walk north' }))
    const north = within(showcase).getByRole('img', { name: 'Orc walking north, 16 frames' })
    expect(north.style.getPropertyValue('--sheet')).toContain('orc-walk-n')
    expect(north.style.getPropertyValue('--duration')).toBe('1504ms')
    expect(within(directions).getByRole('button', { name: 'Walk north' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(directions).getByRole('button', { name: 'Walk west' }))
    const west = within(showcase).getByRole('img', { name: 'Orc walking west, 16 frames' })
    expect(west.style.getPropertyValue('--sheet')).toContain('orc-walk-w')
    expect(west.style.getPropertyValue('--duration')).toBe('1744ms')
    await user.click(within(showcase).getByRole('button', { name: 'Pause' }))
    expect(west).toHaveAttribute('data-playing', 'false')
    for (const small of showcase.querySelectorAll('.sprite-showcase__sprite--small')) expect(small).toHaveAttribute('data-playing', 'false')
    await user.click(within(showcase).getByRole('button', { name: 'Dark' }))
    expect(showcase).toHaveClass('sprite-showcase--dark')
    expect(within(showcase).getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('the welcome page starts the showcase paused for people who prefer reduced motion', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(prefers-reduced-motion: reduce)' }))
    renderPage(<WelcomePage />)
    expect(screen.getByRole('img', { name: 'Orc walking southeast, 16 frames' })).toHaveAttribute('data-playing', 'false')
    expect(screen.getByRole('button', { name: 'Play' })).toBeInTheDocument()
    vi.unstubAllGlobals()
  })

  it('the welcome page pauses the showcase when reduced motion is turned on while it is open', () => {
    const listeners: Array<(event: { matches: boolean }) => void> = []
    const notify = (event: { matches: boolean }) => listeners.forEach((listener) => listener(event))
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: (_: string, listener: (event: { matches: boolean }) => void) => { listeners.push(listener) }, removeEventListener: () => {} }))
    renderPage(<WelcomePage />)
    const sprite = screen.getByRole('img', { name: 'Orc walking southeast, 16 frames' })
    expect(sprite).toHaveAttribute('data-playing', 'true')
    act(() => notify({ matches: true }))
    expect(sprite).toHaveAttribute('data-playing', 'false')
    vi.unstubAllGlobals()
  })

  it('the welcome page shows a real scene flight and the WYRM intro', () => {
    renderPage(<WelcomePage />)
    fireEvent.click(screen.getByRole('tab', { name: /Scene flights/ }))
    const flight = screen.getByRole('figure', { name: 'Example scene flight' })
    const flightVideo = within(flight).getByLabelText(/Scene flight from the village street/) as HTMLVideoElement
    expect(flightVideo.autoplay).toBe(true)
    expect(flightVideo.loop).toBe(true)
    expect(flightVideo.getAttribute('src')).toBe(`${MEDIA_BASE}/welcome/flight.mp4`)
    expect(flightVideo.getAttribute('poster')).toBe(`${MEDIA_BASE}/welcome/flight-poster.jpg`)
    expect(within(flight).getByRole('img', { name: /^Start:/ })).toHaveAttribute('src', `${MEDIA_BASE}/welcome/flight-from.jpg`)
    expect(within(flight).getByRole('img', { name: /^End:/ })).toHaveAttribute('src', `${MEDIA_BASE}/welcome/flight-to.jpg`)
    fireEvent.click(screen.getByRole('tab', { name: /Movies/ }))
    const intro = within(screen.getByRole('figure', { name: 'Example video' })).getByLabelText(/WYRM intro/) as HTMLVideoElement
    // The WYRM page's own media keep their URLs; only the captions live under the media base.
    expect(intro.getAttribute('src')).toBe('/wyrm/media/wyrm-trailer-v5.mp4')
    expect(intro.getAttribute('poster')).toBe('/wyrm/media/trailer-poster.jpg')
    expect(intro.getAttribute('preload')).toBe('none')  // 10 MB only when someone presses play
    expect(intro.autoplay).toBe(false)
    const captions = intro.querySelector('track')!
    expect(captions).toHaveAttribute('kind', 'captions')
    expect(captions.getAttribute('src')).toBe(`${MEDIA_BASE}/welcome/wyrm-intro.en.vtt`)
  })

  it('the scene flight pauses when reduced motion is turned on while it plays', () => {
    const listeners: Array<(event: { matches: boolean }) => void> = []
    const notify = (event: { matches: boolean }) => listeners.forEach((listener) => listener(event))
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener: (_: string, listener: (event: { matches: boolean }) => void) => { listeners.push(listener) }, removeEventListener: () => {} }))
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    renderPage(<WelcomePage />)
    fireEvent.click(screen.getByRole('tab', { name: /Scene flights/ }))
    act(() => notify({ matches: true }))
    expect(pause).toHaveBeenCalled()
    pause.mockRestore()
    vi.unstubAllGlobals()
  })

  it('the scene flight waits for a click when reduced motion is preferred', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(prefers-reduced-motion: reduce)' }))
    renderPage(<WelcomePage />)
    fireEvent.click(screen.getByRole('tab', { name: /Scene flights/ }))
    expect((screen.getByLabelText(/Scene flight from the village street/) as HTMLVideoElement).autoplay).toBe(false)
    vi.unstubAllGlobals()
  })

  it('the welcome page leads with four modes, the characters first', () => {
    renderPage(<WelcomePage />)
    const tabs = within(screen.getByRole('tablist', { name: 'Modes' })).getAllByRole('tab')
    expect(tabs.map((tab) => tab.querySelector('strong')!.textContent)).toEqual(['Characters', 'Movies', 'Scene flights', 'Agents'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', tabs[0].id)
    fireEvent.click(tabs[3])
    const agent = screen.getByRole('figure', { name: 'Example agent session' })
    expect(agent).toHaveTextContent('claude mcp add magic-studios')
    expect(within(agent).getByRole('link', { name: 'Connect an agent' })).toHaveAttribute('href', '/guide#agents')
  })

  it('the modes advance on their own, hold while hovered and stop for good once one is picked', () => {
    vi.useFakeTimers()
    renderPage(<WelcomePage />)
    const showcase = screen.getByRole('region', { name: 'What Magic Studios makes' })
    const selected = () => within(showcase).getAllByRole('tab').find((tab) => tab.getAttribute('aria-selected') === 'true')!.querySelector('strong')!.textContent
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    expect(selected()).toBe('Movies')
    fireEvent.mouseEnter(showcase)
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 3))
    expect(selected()).toBe('Movies')
    fireEvent.mouseLeave(showcase)
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    expect(selected()).toBe('Scene flights')
    fireEvent.click(within(showcase).getByRole('tab', { name: /Characters/ }))
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 3))
    expect(selected()).toBe('Characters')
    expect(within(showcase).getByRole('button', { name: 'Cycle examples' })).toBeInTheDocument()
    vi.useRealTimers()
  })

  it('the modes can be stopped and restarted, and move with the arrow keys', () => {
    vi.useFakeTimers()
    renderPage(<WelcomePage />)
    const showcase = screen.getByRole('region', { name: 'What Magic Studios makes' })
    fireEvent.click(within(showcase).getByRole('button', { name: 'Stop cycling' }))
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 2))
    expect(within(showcase).getByRole('tab', { name: /Characters/ })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(within(showcase).getByRole('tab', { name: /Characters/ }), { key: 'ArrowLeft' })
    expect(within(showcase).getByRole('tab', { name: /Agents/ })).toHaveAttribute('aria-selected', 'true')
    expect(within(showcase).getByRole('tab', { name: /Agents/ })).toHaveFocus()
    fireEvent.click(within(showcase).getByRole('button', { name: 'Cycle examples' }))
    // Keyboard focus inside the showcase holds it; moving focus away lets it cycle again.
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    expect(within(showcase).getByRole('tab', { name: /Agents/ })).toHaveAttribute('aria-selected', 'true')
    act(() => (document.activeElement as HTMLElement).blur())
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    expect(within(showcase).getByRole('tab', { name: /Characters/ })).toHaveAttribute('aria-selected', 'true')
    vi.useRealTimers()
  })

  it('the modes keep the time already shown through a hold, and hover and focus hold independently', () => {
    vi.useFakeTimers()
    renderPage(<WelcomePage />)
    const showcase = screen.getByRole('region', { name: 'What Magic Studios makes' })
    const selected = () => within(showcase).getAllByRole('tab').find((tab) => tab.getAttribute('aria-selected') === 'true')!.querySelector('strong')!.textContent
    act(() => vi.advanceTimersByTime(ADVANCE_MS - 1000))
    fireEvent.mouseEnter(showcase)
    fireEvent.focus(within(showcase).getByRole('button', { name: 'Stop cycling' }))
    fireEvent.mouseLeave(showcase)
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 2))
    expect(selected()).toBe('Characters')
    fireEvent.blur(within(showcase).getByRole('button', { name: 'Stop cycling' }))
    act(() => vi.advanceTimersByTime(1000))
    expect(selected()).toBe('Movies')
    for (const tab of within(showcase).getAllByRole('tab')) expect(tab).toHaveAttribute('aria-controls', 'mode-panel')
    expect(within(showcase).getByRole('tabpanel')).toHaveAttribute('id', 'mode-panel')
    vi.useRealTimers()
  })

  it('the modes hold while the movie plays', () => {
    vi.useFakeTimers()
    renderPage(<WelcomePage />)
    const showcase = screen.getByRole('region', { name: 'What Magic Studios makes' })
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    fireEvent.play(within(showcase).getByLabelText(/WYRM intro/))
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 3))
    expect(within(showcase).getByRole('tab', { name: /Movies/ })).toHaveAttribute('aria-selected', 'true')
    fireEvent.pause(within(showcase).getByLabelText(/WYRM intro/))
    act(() => vi.advanceTimersByTime(ADVANCE_MS))
    expect(within(showcase).getByRole('tab', { name: /Scene flights/ })).toHaveAttribute('aria-selected', 'true')
    vi.useRealTimers()
  })

  it('the modes never advance for people who prefer reduced motion', () => {
    vi.useFakeTimers()
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(prefers-reduced-motion: reduce)' }))
    renderPage(<WelcomePage />)
    act(() => vi.advanceTimersByTime(ADVANCE_MS * 3))
    expect(screen.getByRole('tab', { name: /Characters/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: 'Cycle examples' })).toBeInTheDocument()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('the guide lists every section it contains', () => {
    renderPage(<GuidePage />)
    const contents = screen.getByRole('navigation', { name: 'Contents' })
    for (const link of contents.querySelectorAll('a')) {
      const id = link.getAttribute('href')!.slice(1)
      expect(document.getElementById(id), id).not.toBeNull()
    }
  })

  it.each([
    ['welcome page', <WelcomePage key="welcome" />],
    ['about page', <AboutPage key="about" />],
  ])('the %s says the studio is built for agents and links to how', (_, page) => {
    renderPage(page)
    expect(screen.getByRole('heading', { name: 'Built for agents' })).toBeInTheDocument()
    expect(screen.getByText(/MCP server/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'agents and the API' })).toHaveAttribute('href', '/guide#agents')
  })

  it('the guide tells an agent where the API is, how to sign requests and that MCP runs locally', () => {
    renderPage(<GuidePage />)
    const agents = screen.getByRole('region', { name: 'Agents and the API' })
    expect(within(agents).getByText('https://api.magicstudios.ai')).toBeInTheDocument()
    expect(within(agents).getByText('Authorization: Bearer mvs_…')).toBeInTheDocument()
    expect(within(agents).getByRole('link', { name: 'API documentation' })).toHaveAttribute('href', '/studio/api.md')
    expect(within(agents).getByRole('link', { name: 'API reference' })).toHaveAttribute('href', '/studio/reference')
    expect(within(agents).getByText(/There is no hosted MCP endpoint/)).toBeInTheDocument()
    expect(within(agents).getByText(/^Magic Studios is built for agents: everything in the studio is an API call\./)).toBeInTheDocument()
    for (const mode of ['Animate a character.', 'Make a movie.', 'Fly through a scene.']) expect(within(agents).getByText(mode)).toBeInTheDocument()
    expect(within(agents).getByText('uv tool install https://magicstudios.ai/studio/downloads/magic_studios_mcp-0.2.2-py3-none-any.whl')).toBeInTheDocument()
    expect(within(agents).getByText(/^claude mcp add magic-studios/)).toBeInTheDocument()
    expect(within(agents).getByRole('link', { name: 'Magic Studios skill' })).toHaveAttribute('href', 'https://magicstudios.ai/studio/downloads/magic-studios-skill-0.2.2.zip')
  })

  it('the guide opens at the section a link from another page names', () => {
    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView
    render(<MemoryRouter initialEntries={['/guide#agents']}><GuidePage /></MemoryRouter>)
    expect(scrollIntoView.mock.contexts[0]).toBe(document.getElementById('agents'))
    delete (Element.prototype as Partial<Element>).scrollIntoView
  })

  it('the guide still renders when the fragment is malformed', () => {
    render(<MemoryRouter initialEntries={['/guide#100%']}><GuidePage /></MemoryRouter>)
    expect(screen.getByRole('heading', { name: 'Guide', level: 1 })).toBeInTheDocument()
  })

  it('the storage policy describes per-person links and self-service deletion', () => {
    renderPage(<StoragePolicy />)
    expect(screen.getByRole('heading', { name: 'Deleting your work' })).toBeInTheDocument()
    expect(screen.getByText(/there are no shareable download links/)).toBeInTheDocument()
    expect(screen.queryByText(/temporary signed links/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Automatic deletion of inactive work' })).toBeInTheDocument()
  })
})

describe('the welcome page in a build without hosted media', () => {
  afterEach(() => { vi.stubEnv('VITE_SITE_MEDIA_BASE', MEDIA_BASE) })

  /** Public and self-hosted builds leave the base unset: load the page fresh, as such a build does. */
  async function renderWithoutMedia(base = '') {
    vi.stubEnv('VITE_SITE_MEDIA_BASE', base)
    vi.resetModules()
    const [{ WelcomePage: Page }, { MemoryRouter: Router }] = await Promise.all([import('./WelcomePage'), import('react-router-dom')])
    return render(<Router><Page /></Router>)
  }

  it.each([['unset', ''], ['blank', '  ']])('leaves out every media demo when the base is %s, so nothing renders broken', async (_, base) => {
    const { container } = await renderWithoutMedia(base)
    expect(container.querySelectorAll('video, img, source, track, [style*="--sheet"]')).toHaveLength(0)
    for (const name of ['Example animation made in Magic Studios', 'Example scene flight', 'Example video']) {
      expect(screen.queryByRole('figure', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Stop cycling|Cycle examples|Pause|Play/ })).not.toBeInTheDocument()
  })

  it('still lists the four modes and shows the agent session, which needs no media', async () => {
    await renderWithoutMedia()
    const showcase = screen.getByRole('region', { name: 'What Magic Studios makes' })
    const modes = within(within(showcase).getByRole('list', { name: 'Modes' })).getAllByRole('listitem')
    expect(modes.map((mode) => mode.querySelector('strong')!.textContent)).toEqual(['Characters', 'Movies', 'Scene flights', 'Agents'])
    expect(modes[0]).toHaveTextContent('One image becomes animations in eight directions.')
    const agent = within(showcase).getByRole('figure', { name: 'Example agent session' })
    expect(agent).toHaveTextContent('claude mcp add magic-studios')
    expect(within(agent).getByRole('link', { name: 'Connect an agent' })).toHaveAttribute('href', '/guide#agents')
  })

  it('keeps the rest of the page: the pitch, the sign-up links and every section', async () => {
    await renderWithoutMedia()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('A free AI game asset studio for agents')
    expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F&signup=true')
    for (const name of ['Built for agents', 'Where it runs', 'Your files are private']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.getByRole('navigation', { name: 'Magic Studios' })).toBeInTheDocument()
    expect(screen.getByRole('contentinfo')).toBeInTheDocument()
  })
})
