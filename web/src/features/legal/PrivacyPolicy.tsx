import { Link } from 'react-router-dom'

import { PublicFooter, PublicNav } from '../welcome/PublicNav'

const contact = <a href="mailto:admin@magicstudios.ai">admin@magicstudios.ai</a>

/** `/privacy`: what Magic Video Studio collects, who processes it and how to delete it. */
export function PrivacyPolicy() {
  return (
    <>
      <PublicNav title="Privacy policy" />
      <main className="storage-policy" id="main-content" tabIndex={-1}>
        <header>
          <h1>Privacy policy</h1>
          <p>What Magic Video Studio collects, why, who processes it, and how to delete it.</p>
          <p className="storage-policy__updated">Updated November 2, 2026</p>
        </header>
        <section aria-labelledby="who-we-are">
          <h2 id="who-we-are">Who we are</h2>
          <p>Magic Video Studio at magicstudios.ai is operated by Boundless Games, LLC. For privacy questions, deletion requests or anything else in this policy, email {contact}.</p>
          <p>You must be 18 or older to use the studio. If we learn that an account belongs to someone younger, we delete it.</p>
        </section>
        <section aria-labelledby="account-data">
          <h2 id="account-data">Your account</h2>
          <p>Sign-in is handled by Auth0 (Okta). We receive and store your email address. We keep a user record, your sign-in sessions and short-lived sign-in tokens. If you sign up with an email and password, Auth0 keeps the password; we never see it.</p>
          <p>We set a single, strictly necessary session cookie. We use no advertising or tracking cookies. The studio also keeps a few conveniences in your browser&rsquo;s local storage, such as your most recent project and unsaved frame edits; they stay on your device.</p>
        </section>
        <section aria-labelledby="access-requests">
          <h2 id="access-requests">Access requests</h2>
          <p>We no longer take requests for access or send invitations. Anyone can sign up, and our shared machines are only for a small allowlist we choose.</p>
          <p>If you asked for access or were invited before, we still have your email address, when you asked and when you were invited. It gives you no access. We keep it only so we can review past requests, and delete it when we dismiss it or a year after you last asked or were invited, whichever comes first. Email {contact} to have it removed sooner.</p>
        </section>
        <section aria-labelledby="content-data">
          <h2 id="content-data">Your content</h2>
          <p>We store the images and audio you upload, the media we generate for you, the prompts and settings you write, your project, movie and scene flight data, and your generation history.</p>
          <p>It is stored in Google Cloud (Cloud Storage and Cloud SQL) in the United States (us-west1).</p>
        </section>
        <section aria-labelledby="keys-machines">
          <h2 id="keys-machines">Keys and machines</h2>
          <p>A Higgsfield API key you save is stored encrypted and never shown again; only its last characters are displayed. A studio API key is stored only as a hash and is shown once, when you create it.</p>
          <p>For a machine you attach, we store its name, its status and a credential that is rotated automatically. The machine receives only its owner&rsquo;s jobs and the inputs needed to run them.</p>
        </section>
        <section aria-labelledby="tips">
          <h2 id="tips">Tips</h2>
          <p>Tips are paid on Stripe&rsquo;s checkout page, and you don&rsquo;t need an account to leave one. Stripe collects your payment details, your email address for the receipt and any other details its page asks for, under <a href="https://stripe.com/privacy">Stripe&rsquo;s privacy policy</a>. We never see your card.</p>
          <p>From Stripe we keep the amount, the currency, the time and Stripe&rsquo;s reference numbers for the payment. We don&rsquo;t keep your name, email or address. If you were signed in when you tipped, we link the tip to your account. Deleting your account removes that link; we keep the payment record for our accounts. We don&rsquo;t show tips or who left them anywhere.</p>
        </section>
        <section aria-labelledby="processing">
          <h2 id="processing">Where your work is processed</h2>
          <p>Generation runs in one of three places:</p>
          <ul>
            <li>on a machine you attach, using your own GPU;</li>
            <li>on our own GPU machines, for accounts on our allowlist;</li>
            <li>at Higgsfield, if you save your Higgsfield key and choose it. Higgsfield then processes the inputs under your own Higgsfield account and Higgsfield&rsquo;s terms, and bills you directly.</li>
          </ul>
          <p>Movie planning (the director), for accounts on our allowlist, sends your brief to an AI model through OpenRouter.</p>
        </section>
        <section aria-labelledby="screening">
          <h2 id="screening">Prompt safety screening</h2>
          <p>Before any generation runs, the text prompts you submit are sent to an AI classification model through OpenRouter, and the model provider it routes to, to check them against the acceptable use rules in our <Link className="policy-link" to="/terms">terms of service</Link>. Screening does not run on uploaded images.</p>
          <p>If a prompt is refused, we keep a record of the refusal: your account, the prompt text, the category and the time. We keep it for up to one year to investigate and prevent abuse. This record is kept even if you delete your account, and is used for that purpose only.</p>
        </section>
        <section aria-labelledby="monitoring">
          <h2 id="monitoring">Error monitoring</h2>
          <p>Sentry receives error reports and a 5% sample of performance traces from the web app and server. It is configured not to send personal data by default. We do not record session replays.</p>
        </section>
        <section aria-labelledby="visitor-analytics">
          <h2 id="visitor-analytics">Visitor analytics</h2>
          <p>We count visits with Cloudflare Web Analytics, on the public pages and in the studio. It sets no cookies and stores nothing in your browser. It records the page address without its query string, the referring site, your country, and your browser, operating system and device type. It also records page load timings.</p>
          <p>It does not identify you, follow you across other sites, or link a visit to your account. We see only totals, and we use them to understand which pages people use.</p>
        </section>
        <section aria-labelledby="no-sale">
          <h2 id="no-sale">What we do not do</h2>
          <p>We do not sell your personal information or share it for advertising. We do not use your content to train models.</p>
        </section>
        <section aria-labelledby="file-access">
          <h2 id="file-access">Who can open your files</h2>
          <p>Your files are kept in a private bucket. Every preview and download checks that you are the signed-in owner. There are no shareable links.</p>
        </section>
        <section aria-labelledby="retention">
          <h2 id="retention">How long we keep it</h2>
          <p>Your files stay until you delete them, with one exception. A project, movie or scene flight that has not been opened for 90 days and has no release is deleted automatically. During the 14 days before, the studio shows a warning in your project list. Released work is kept. To apply this, we record when you last opened each project, movie and scene flight.</p>
          <p>When work runs on a GPU machine (yours or ours), our job queue keeps a record of each finished job, including the prompt that was run, to operate and audit those machines. These records are not removed when you delete your work or your account; their outputs are.</p>
          <p>You can delete any project, movie or scene flight yourself. You can also delete your whole account from the account menu with <strong>Delete account</strong>. That deletes your projects, movies, flights, saved keys, API keys, attached machines, usage records, sessions and user record, and unlinks any tips from your account.</p>
          <p>After deletion, files remain recoverable by us for 7 days, then are erased. Database backups are kept for about 7 days and then roll off.</p>
          <p>Deleting your account does not remove your Auth0 sign-in identity. Email {contact} to have it removed. Copies you downloaded or delivered elsewhere are yours and are not recalled.</p>
        </section>
        <section aria-labelledby="rights">
          <h2 id="rights">Your choices and rights</h2>
          <p>You can open and export your work in the studio, and delete it as described above. For anything else, email {contact}.</p>
          <p>We respond to verified requests, including requests under state privacy laws such as California&rsquo;s, within the time those laws require.</p>
        </section>
        <section aria-labelledby="security">
          <h2 id="security">Security</h2>
          <p>Data is encrypted in transit. Storage is private, every request is checked against its owner, and provider keys are encrypted. No system is perfectly secure.</p>
        </section>
        <section aria-labelledby="changes">
          <h2 id="changes">Changes to this policy</h2>
          <p>When we change this policy, we update the date on this page. For material changes, we tell signed-in users in the studio before the changes apply.</p>
        </section>
      </main>
      <PublicFooter />
    </>
  )
}
