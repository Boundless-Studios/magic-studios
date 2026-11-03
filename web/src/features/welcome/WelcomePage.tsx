import { Link } from 'react-router-dom'

import { auth0LoginUrl, auth0SignUpUrl } from '../../api/auth'
import { PublicFooter, PublicNav } from './PublicNav'
import { ModeShowcase } from './ModeShowcase'

/** `/welcome`: what the studio is, for someone who is not signed in. A signed-out visit
 * to `/` lands here; deep links still go straight to `/login`. It leads with what the
 * studio makes and with agents, because every action is an API call. */
export function WelcomePage() {
  return (
    <div className="welcome">
      <PublicNav />
      <main id="main-content" tabIndex={-1}>
        <header className="welcome__hero">
          <h1>A free AI game asset studio for agents</h1>
          <p>Animate characters in eight directions. Make movies. Fly a camera through your locations. Work in the browser, or let your AI agent do it through the same API.</p>
          <div className="welcome__actions">
            <a className="button button--primary button--large" href={auth0SignUpUrl('/')}>Sign up</a>
            <Link className="button button--large" to="/guide#agents">Connect an agent</Link>
            <a className="button button--large" href={auth0LoginUrl('/')}>Sign in</a>
          </div>
          <small>Sign up with Google or an email and password. Read the <Link className="policy-link" to="/guide">guide</Link>.</small>
        </header>

        <ModeShowcase />

        <section aria-labelledby="welcome-agents">
          <h2 id="welcome-agents">Built for agents</h2>
          <p className="welcome__text">Everything you can do in the studio is an API call. The web app uses the same API your agent does, so an agent can make characters, movies and scene flights and export them, on the same projects you see. See <Link className="policy-link" to="/guide#agents">agents and the API</Link>.</p>
          <div className="welcome__cards">
            <article>
              <h3>MCP server</h3>
              <p>Install it once and add it to Claude Code, Codex or Claude Desktop. It runs on your computer with your API key.</p>
            </article>
            <article>
              <h3>Agent skill</h3>
              <p>Teaches an agent the studio's steps: generate, review frames, save loops and export.</p>
            </article>
            <article>
              <h3>API reference</h3>
              <p>Every route, field and error, plus an OpenAPI schema. <a className="policy-link" href="/studio/reference">Open the reference</a>.</p>
            </article>
          </div>
        </section>

        <section aria-labelledby="welcome-run">
          <h2 id="welcome-run">Where it runs</h2>
          <div className="welcome__cards welcome__cards--two">
            <article>
              <h3>Your own GPU</h3>
              <p>Attach a supported NVIDIA GPU. It runs only your work. Free.</p>
            </article>
            <article>
              <h3>Your Higgsfield key</h3>
              <p>No GPU? Use your Higgsfield API key. Higgsfield bills you directly. We add no fee.</p>
            </article>
          </div>
          <p className="welcome__note">Movies, scene flights and image generation need a GPU. The studio&rsquo;s shared machines are for a small allowlist and aren&rsquo;t available on request; everyone else uses their own.</p>
        </section>

        <section aria-labelledby="welcome-private">
          <h2 id="welcome-private">Your files are private</h2>
          <ul className="welcome__list">
            <li>Only you can open your files. A copied link shows nothing to anyone else.</li>
            <li>Delete a project or your whole account at any time. Deleted files are erased within 7 days.</li>
            <li>Saved keys are encrypted.</li>
          </ul>
        </section>
      </main>
      <PublicFooter />
    </div>
  )
}
