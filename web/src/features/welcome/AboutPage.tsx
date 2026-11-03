import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { hostedService } from '../../hostedService'
import { PublicFooter, PublicNav } from './PublicNav'

/** `/about`: what the studio is, what it costs, who runs it and how to reach them.
 * `/cost` and `/pricing` redirect here to `#cost`. */
export function AboutPage() {
  const { hash } = useLocation()
  // A redirected `/cost` link lands on the cost section. The page stays mounted when the
  // About link drops the hash, so that returns to the top rather than staying at Cost.
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView()
    else window.scrollTo(0, 0)
  }, [hash])
  return <>
    <PublicNav title="About" />
    <main className="storage-policy" id="main-content" tabIndex={-1}>
      <header><h1>About</h1>
        <p>Magic Studios turns one character image into game-ready animation. It also makes short movies and camera flights through your locations.</p></header>
      <aside className="storage-policy__notice"><strong>Open to everyone</strong>
        <p>Anyone can sign up and use their own GPU or Higgsfield key. The studio’s shared machines are for a small allowlist and aren’t available on request.</p></aside>
      <section aria-labelledby="cost"><h2 id="cost">Cost</h2>
        <p>Magic Studios is free. You bring the compute: your own NVIDIA GPU or your own Higgsfield key. A small allowlist of accounts can also use the studio’s shared machines. There are no paid plans and nothing to buy.</p>
        <h3>Your own GPU: free</h3>
        <p>Attach a computer with a supported NVIDIA GPU. It runs only your work. We don’t charge for it. You pay for the hardware and electricity.</p>
        <h3>Your Higgsfield key: Higgsfield’s prices</h3>
        <p>No GPU? Save your Higgsfield API key and send animations to Higgsfield. Higgsfield bills your account at its own rates. We add no fee.</p>
        <p>You choose where each animation runs. A job sent to your GPU never moves to Higgsfield, and a job sent to Higgsfield never moves to your GPU. We store your key encrypted.</p>
        <h3>Shared machines: allowlisted accounts only</h3>
        <p>An allowlisted account with no machine of its own runs on the studio’s shared machines, including movies, scene flights and image generation, which don’t run on Higgsfield. The pool is small, so work may wait its turn, and the allowlist isn’t open to requests. Everyone else needs their own GPU or Higgsfield key.</p>
        <h3>Limits</h3>
        <p>Each account has a storage limit and a limit on how many jobs run at once. The studio shows your usage.</p>
        <p>Your files are private to you. Unreleased work you haven’t opened in 90 days is deleted after a warning. See the <Link className="policy-link" to="/storage-policy">storage policy</Link>.</p>
        {hostedService && <>
          <h3>Tips</h3>
          <p>If Magic Studios has helped you, you can <Link className="policy-link" to="/support">leave a tip</Link>. A tip is a voluntary gift. It buys nothing, unlocks nothing and isn’t tax-deductible.</p>
        </>}
      </section>
      <section><h2>Who makes it</h2>
        <p>Magic Studios is made by Boundless Games, LLC. We also make <a href="/wyrm/">WYRM</a>, a game in development.</p>
      </section>
      <section><h2>Built for agents</h2>
        <p>Everything you do with a project in the studio also works through an API that we host. An MCP server wraps it for AI agents. You and your agents drive the same studio, with the same projects and limits. See <Link className="policy-link" to="/guide#agents">agents and the API</Link>.</p>
      </section>
      <section><h2>Contact</h2>
        <p>Email <a href="mailto:admin@magicstudios.ai">admin@magicstudios.ai</a>.</p>
      </section>
    </main>
    <PublicFooter />
  </>
}
