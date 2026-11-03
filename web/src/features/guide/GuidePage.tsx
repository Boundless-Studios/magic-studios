import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { PublicFooter, PublicNav } from '../welcome/PublicNav'
import { DownloadChecksums, MCP_WHEEL_URL, SKILL_ZIP_URL } from './DownloadChecksums'

const CLAUDE_DESKTOP_CONFIG = `{
  "mcpServers": {
    "magic-studios": {
      "command": "magic-studios-mcp",
      "env": { "MAGIC_STUDIOS_API_KEY": "mvs_your_key" }
    }
  }
}`

const SECTIONS = [
  ['start', 'Getting started'],
  ['animate', 'Animate a character'],
  ['export', 'Release and export'],
  ['movies', 'Movies'],
  ['flights', 'Scene flights'],
  ['machine', 'Attach your own machine'],
  ['higgsfield', 'Use your Higgsfield key'],
  ['agents', 'Agents and the API'],
  ['rules', 'What you can make'],
  ['data', 'Storage, limits and deletion'],
] as const

/** `/guide`: the public how-to for the studio, readable before signing in. */
export function GuidePage() {
  // A link such as `/guide#agents` from another page renders the guide first, so the
  // browser's own jump to the section has already been missed.
  const { hash } = useLocation()
  useEffect(() => {
    // Section ids are plain words, so the raw fragment is compared as it is; decoding
    // an untrusted fragment such as `#100%` would throw.
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView()
  }, [hash])
  return (
    <>
      <PublicNav title="Guide" />
      <main className="storage-policy guide" id="main-content" tabIndex={-1}>
        <header>
          <h1>Guide</h1>
          <p>From one character image to a finished animation set, plus movies, scene flights and where your work runs. Your projects also work through the <a className="policy-link" href="#agents">API and MCP server</a>.</p>
        </header>
        <nav className="guide__contents" aria-label="Contents">
          <ol>{SECTIONS.map(([id, title]) => <li key={id}><a href={`#${id}`}>{title}</a></li>)}</ol>
        </nav>

        <section aria-labelledby="start">
          <h2 id="start">Getting started</h2>
          <ol>
            <li>Sign in with a verified email address. A new account starts empty.</li>
            <li>Decide where generation runs. Open the account menu and choose <strong>Your machines</strong>: attach your own GPU, save a Higgsfield key, or both. See <a href="#machine">Attach your own machine</a> and <a href="#higgsfield">Use your Higgsfield key</a>.</li>
            <li>On <strong>Your projects</strong>, choose <strong>New project</strong>. The setup starts with a character; it also offers <strong>Make a movie instead</strong> and <strong>Make a scene flight instead</strong>.</li>
          </ol>
        </section>

        <section aria-labelledby="animate">
          <h2 id="animate">Animate a character</h2>
          <p>Setup takes five short steps. Creating the workspace saves your choices; it does not start any generation.</p>
          <ol>
            <li><strong>Artwork.</strong> Upload one PNG, JPEG or WebP image of your character and name the project. The studio keeps your camera angle and pose.</li>
            <li><strong>Game view.</strong> Pick Single direction, Side-scrolling, Four-direction or Eight-direction. Every suggestion stays editable, or tick <em>Start with just this image</em>.</li>
            <li><strong>Facing.</strong> Tell the studio which screen heading your image shows. Left and right never flip your pixels.</li>
            <li><strong>Animations.</strong> Tick only what you want: Idle, Walk, Run, Jump, Attack, Guard, Cast, Hurt, Death, Trip, or your own actions. An empty set is fine.</li>
            <li><strong>Review</strong> and choose <strong>Create workspace</strong>.</li>
          </ol>
          <p>The workspace has three parts: <strong>Subject</strong> (your character art), <strong>Motions</strong> and <strong>Release &amp; export</strong>.</p>
          <p>In <strong>Motions</strong>, select the animations to make, choose where they run and press <strong>Generate</strong>. Each result is a take you can review, retry or keep. Looping animations get a suggested loop, and you can change frames and timing by hand. Framing and the pivot point are set once for the whole clip, so the character never jumps between frames.</p>
          <p>Each direction needs its own artwork before its animations can be generated. The studio never mirrors a direction: left and right are separate art. If you only have a front or front three-quarter image, the studio can turn your character in place to draft the other directions, and a draft is used only after you check its facing, equipment and identity. You can add, remove or change directions and animations later; older takes stay in history.</p>
        </section>

        <section aria-labelledby="export">
          <h2 id="export">Release and export</h2>
          <p>When the set looks right, open <strong>Release &amp; export</strong>. A release is a permanent record of the character and the takes you approved; later edits never change it.</p>
          <p>Export a release as <strong>PNG + JSON</strong>: production-ready PNG frames with a JSON manifest describing each animation, direction and timing. Download links work only for you.</p>
        </section>

        <section aria-labelledby="movies">
          <h2 id="movies">Movies</h2>
          <ol>
            <li><strong>Movie.</strong> Write the brief and a prompt for each shot, upload or compose starting images and arrange the sequence. <strong>Generate movie</strong> makes any missing shots and stitches them in story order. <strong>Generate another take</strong> retries a shot and keeps earlier takes in history.</li>
            <li><strong>Cut.</strong> Adjust order, trims, music and narration, then assemble.</li>
            <li><strong>Release &amp; export.</strong> Review the assembled movie and export a release.</li>
          </ol>
          <p>Keep each shot to one physical action. Under <strong>Motion direction</strong>, describe one camera move, such as &ldquo;The camera pushes in with small amplitude at slow speed&rdquo;, and name what must stay still.</p>
        </section>

        <section aria-labelledby="flights">
          <h2 id="flights">Scene flights</h2>
          <p>A scene flight is a camera journey across location images. Each leg is one clip that starts on an origin image and ends exactly on a destination image. When one leg ends on the image the next begins from, the legs play as one continuous move. Legs run from 5 to 15 seconds.</p>
        </section>

        <section aria-labelledby="machine">
          <h2 id="machine">Attach your own machine</h2>
          <p>A machine you attach runs only your work, never anyone else&rsquo;s. You pay for your hardware and electricity; the studio charges nothing for its time.</p>
          <ol>
            <li>Open the account menu, choose <strong>Your machines</strong>, then <strong>Add a machine</strong>. Pick one of the listed configurations and enter the machine&rsquo;s local ComfyUI address.</li>
            <li>Copy the command it shows and run it once on that machine (Linux, or WSL on Windows). It installs a small worker in your home folder, attaches the machine and starts it as a service that survives reboots. It does not need root.</li>
          </ol>
          <p>The machine needs a supported NVIDIA GPU and ComfyUI with that configuration&rsquo;s models and nodes already installed; attaching checks them first. The command works once and expires after an hour. The machine renews its own credential, so there is nothing for you to keep. If it stays off for more than a month, remove it and attach it again.</p>
        </section>

        <section aria-labelledby="higgsfield">
          <h2 id="higgsfield">Use your Higgsfield key</h2>
          <p>Without a GPU, you can send character animations to Higgsfield. Under <strong>Your machines</strong>, paste your Higgsfield API key into <strong>Bring your Higgsfield key</strong> and save it. When you generate, choose Higgsfield as where it runs.</p>
          <p>Higgsfield bills your Higgsfield account at its own rates. Magic Studios adds no fee and never handles that payment. Your key is stored encrypted and only its last characters are ever shown. Work you send to your machine never switches to Higgsfield, or the other way round.</p>
          <p>Movies, scene flights and image generation don&rsquo;t run on Higgsfield. A public account needs its own machine for them; an invited account can also use the studio&rsquo;s shared machines.</p>
        </section>

        <section aria-labelledby="agents">
          <h2 id="agents">Agents and the API</h2>
          <p>Magic Studios is built for agents: everything in the studio is an API call. We host the API, and an MCP server turns it into tools for AI agents. You, your scripts and your agents work in the same studio, on the same projects.</p>

          <h3>Create an API key</h3>
          <ol>
            <li>Sign in. In the account menu, choose <strong>API keys</strong>, name a key and create it.</li>
            <li>Copy the key. It starts with <code>mvs_</code> and is shown only once.</li>
            <li>Send it with every request to <code>https://api.magicstudios.ai</code> as <code>Authorization: Bearer mvs_…</code>.</li>
          </ol>
          <p>A key acts as you and sees only your projects. Revoke it under <strong>API keys</strong> and it stops working at once.</p>

          <h3>Connect an agent</h3>
          <p>Any agent that can make HTTP requests can use the API. Give it your key and the <a href="/studio/api.md">API documentation</a>, which starts with a short example. The <a href="/studio/reference">API reference</a> lists every route with its fields and errors, and the <a href="/studio/openapi.json">OpenAPI schema</a> suits client generators.</p>
          <p>An MCP-capable agent, such as Claude Code, Claude Desktop or Codex, can use the MCP server instead. It runs on your computer and calls the API with your key. There is no hosted MCP endpoint.</p>

          <h3>Install the MCP server</h3>
          <ol>
            <li>Install it with <a href="https://docs.astral.sh/uv/">uv</a>, or with pipx. It needs Python 3.13 or later, which uv can install for you.
              <pre><code>{`uv tool install ${MCP_WHEEL_URL}`}</code></pre>
              <pre><code>{`pipx install ${MCP_WHEEL_URL}`}</code></pre>
            </li>
            <li>Add it to your agent, with your key in place of <code>mvs_your_key</code>. For Claude Code:
              <pre><code>claude mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_your_key -- magic-studios-mcp</code></pre>
              For Codex:
              <pre><code>codex mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_your_key -- magic-studios-mcp</code></pre>
              For Claude Desktop, add this to <code>claude_desktop_config.json</code> and restart the app:
              <pre><code>{CLAUDE_DESKTOP_CONFIG}</code></pre>
            </li>
            <li>Start a new session so the agent loads the server.</li>
          </ol>

          <h3>Add the skill</h3>
          <p>The <a href={SKILL_ZIP_URL}>Magic Studios skill</a> tells an agent how to work in all three modes step by step: animate a character, make a movie and fly through a scene. The zip holds one folder, <code>magic-studios</code>. For Claude Code, unzip it into <code>~/.claude/skills/</code>. For Codex, unzip it into <code>~/.agents/skills/</code>. For Claude Desktop, upload the zip under Settings → Capabilities → Skills.</p>
          <DownloadChecksums />

          <h3>What an agent can do</h3>
          <ul>
            <li><strong>Animate a character.</strong> Upload or generate the character, generate animations, look at decoded frames, find and save a loop, match the attack to the walk, and release and download PNG frames with a JSON manifest.</li>
            <li><strong>Make a movie.</strong> Set the cast and voices, write the shots, give each shot that doesn&rsquo;t continue another an opening image and each dialogue shot its plate, build the movie, retry and choose takes, cut it with music and narration, then release and download the MP4.</li>
            <li><strong>Fly through a scene.</strong> Add location images, define the camera legs between them, generate and choose takes, chain the legs into one shot and download it.</li>
          </ul>
          <p>Generation still runs on your own machine or your Higgsfield key, within your account&rsquo;s limits. A key cannot create keys, delete your account, attach machines or save a Higgsfield key. Do those in the browser.</p>
        </section>

        <section aria-labelledby="rules">
          <h2 id="rules">What you can make</h2>
          <p>Every prompt is checked against our <Link className="policy-link" to="/terms">acceptable use rules</Link> before anything is generated. Fictional violence, combat, gore, horror and dark themes are fine. Sexual content, anything involving minors, degrading or deceptive depictions of real people, harassment, extremist content and encouragement of self-harm are refused, and the error says which rule applies.</p>
        </section>

        <section aria-labelledby="data">
          <h2 id="data">Storage, limits and deletion</h2>
          <ul>
            <li>Each account has a storage allowance and a limit on work running at once. The studio shows your current use.</li>
            <li>Only you can open your files. Previews and downloads check your sign-in on every request.</li>
            <li>To delete a project, movie or scene flight, use <strong>Delete</strong> on it in <strong>Your projects</strong> and type its name to confirm. Its files, takes, releases and exports are erased.</li>
            <li>To delete everything, choose <strong>Delete account</strong> in the account menu and type your email address. This also removes saved keys and attached machines, and signs you out.</li>
            <li>A project, movie or scene flight nobody opens for 90 days is deleted automatically unless it has a release. <strong>Your projects</strong> shows the date during the last 14 days; opening it resets the clock.</li>
          </ul>
          <p>Details are in the <Link className="policy-link" to="/storage-policy">storage policy</Link> and on the <Link className="policy-link" to="/about#cost">cost</Link> section.</p>
        </section>

      </main>
      <PublicFooter />
    </>
  )
}
