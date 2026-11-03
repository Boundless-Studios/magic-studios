import { Link } from 'react-router-dom'

import { PublicFooter, PublicNav } from '../welcome/PublicNav'

export function StoragePolicy() {
  return (
    <>
      <PublicNav title="Storage policy" />
      <main className="storage-policy" id="main-content" tabIndex={-1}>
        <header>
          <h1>Storage policy</h1>
          <p>How we store your uploads, generated assets and exports, who can reach them, and how to delete them.</p>
          <p className="storage-policy__updated">Updated October 20, 2026</p>
        </header>
        <aside className="storage-policy__notice" aria-label="Current availability">
          <strong>Current storage behavior</strong>
          <p>Your files stay until you delete them, except that unreleased work nobody opens for 90 days is deleted automatically, after a warning. We do not offer an archive or backup service for your files, so keep your own copies of important source files and exported work.</p>
        </aside>
        <section aria-labelledby="stored-files">
          <h2 id="stored-files">What we store</h2>
          <p>Magic Video Studio keeps uploaded references, prepared images, generated assets and export files in a private Google Cloud Storage bucket. Project details and generation history, including the prompts you write, are stored separately in the application database.</p>
          <p>A Higgsfield API key you save is stored encrypted and is never shown again; only its last characters are displayed.</p>
        </section>
        <section aria-labelledby="file-access">
          <h2 id="file-access">Who can access your files</h2>
          <p>The storage bucket is private and is never exposed directly. Every preview, download and export goes through the studio, which checks on every request that you are signed in as the owner. A copied link shows nothing to anyone else, and there are no shareable download links.</p>
          <p>Uploads use a link that expires after 15 minutes and can create its file only once.</p>
          <p>The machine or provider that runs a generation receives the inputs it needs. A machine you attach receives only your own work. Work you send to Higgsfield is processed under your own Higgsfield account and Higgsfield&rsquo;s terms, and this policy does not cover copies Higgsfield keeps.</p>
        </section>
        <section aria-labelledby="storage-limits">
          <h2 id="storage-limits">Storage allowance</h2>
          <p>Managed storage is limited per account. New uploads and generation may be blocked when your allowance is reached; existing work can still be exported and downloaded. The studio shows your current use and limits.</p>
          <p>When you replace a file, we keep up to three earlier versions of it.</p>
        </section>
        <section aria-labelledby="deletion">
          <h2 id="deletion">Deleting your work</h2>
          <p>You can delete a project, movie or scene flight from <strong>Your projects</strong> after typing its name to confirm. This erases every stored version of its files, and its takes, releases, exports and history. Anything still running for it is cancelled first.</p>
          <p>You can delete your whole account from the account menu after typing your email address. This also removes saved Higgsfield keys, API keys, attached machines (which can no longer connect), your usage records and your sign-in sessions.</p>
          <p>After deletion, files stay recoverable by us for 7 days, in case of a mistake or an outage, and are then erased. Database backups, kept for about a week, roll off on their own. Packages you have already delivered or downloaded are your own copies and are not recalled.</p>
        </section>
        <section aria-labelledby="expiry">
          <h2 id="expiry">Automatic deletion of inactive work</h2>
          <p>A project, movie or scene flight that nobody has opened for 90 days is deleted automatically, the same way as if you had deleted it. During the last 14 days, <strong>Your projects</strong> shows the date it will be deleted; opening it resets the 90 days.</p>
          <p>Work with a release is kept. Work that existed when this started was given a full 90 days.</p>
        </section>
        <section aria-labelledby="own-storage">
          <h2 id="own-storage">Using your own storage</h2>
          <p>Connecting your own Google Cloud Storage bucket is not currently available. Attaching your own GPU does not change where your project files are stored.</p>
        </section>
        <section aria-labelledby="prompt-records">
          <h2 id="prompt-records">Prompts we refuse</h2>
          <p>Prompts are checked against our <Link className="policy-link" to="/terms">acceptable use rules</Link> before anything is generated. If one is refused, we keep the refused text with your account and the reason for up to a year to investigate abuse, even if you delete your account. See the <Link className="policy-link" to="/privacy">privacy policy</Link>.</p>
        </section>
        <section aria-labelledby="planned-retention">
          <h2 id="planned-retention">Changes</h2>
          <p>We may add paid storage options or change the retention periods. We will update this page, and tell you in the studio, before a change applies to your work.</p>
        </section>
      </main>
      <PublicFooter />
    </>
  )
}
