import { Link } from 'react-router-dom'

import { hostedService } from '../../hostedService'
import { PublicFooter, PublicNav } from '../welcome/PublicNav'

const contact = <a href="mailto:admin@magicstudios.ai">admin@magicstudios.ai</a>

/** `/terms`: the terms of service, including the acceptable use rules (1-6 are screened automatically). */
export function TermsOfService() {
  return (
    <>
      <PublicNav title="Terms of service" />
      <main className="storage-policy" id="main-content" tabIndex={-1}>
        <header>
          <h1>Terms of service</h1>
          <p>The rules for using Magic Video Studio, including what you may not make with it.</p>
          <p className="storage-policy__updated">Updated October 21, 2026</p>
        </header>
        <section aria-labelledby="agreement">
          <h2 id="agreement">Who we are</h2>
          <p>Magic Video Studio at magicstudios.ai is operated by Boundless Games, LLC. By using it, you agree to these terms and to our <Link className="policy-link" to="/privacy">privacy policy</Link>.</p>
        </section>
        <section aria-labelledby="eligibility">
          <h2 id="eligibility">Who can use it</h2>
          <p>You must be 18 or older. If we learn that an account belongs to someone younger, we delete it.</p>
          <p>Keep your sign-in, API keys and machine credentials to yourself. You are responsible for what is done with your account.</p>
        </section>
        <section aria-labelledby="availability">
          <h2 id="availability">The service</h2>
          <p>The studio is in early access and is provided &ldquo;as is&rdquo;. We may change or discontinue features, and the service may be unavailable at times.</p>
          <p>Unreleased projects, movies and scene flights that nobody opens for 90 days are deleted automatically, after a warning in the studio; see the <Link className="policy-link" to="/storage-policy">storage policy</Link>. Keep your own copies of work that matters.</p>
        </section>
        <section aria-labelledby="fees">
          <h2 id="fees">Fees</h2>
          <p>Using the studio with your own GPU or your own Higgsfield key is free today. Higgsfield bills you directly for work you send to it.</p>
          <p>There are no paid features. See <Link className="policy-link" to="/about#cost">cost</Link>.</p>
        </section>
        {hostedService && (
          <section aria-labelledby="tips">
            <h2 id="tips">Tips</h2>
            <p>If you use the studio and want to support it, you can <Link className="policy-link" to="/support">leave a tip</Link>. A tip is a voluntary, one-time payment through Stripe for the free service you have used. You get no further goods, services, features or priority in return, and it does not change your account.</p>
            <p>Tips are not refundable. Boundless Games, LLC is a business, not a charity: a tip is not a charitable donation and is not tax-deductible. If you were charged by mistake, such as twice for one tip, email {contact}.</p>
          </section>
        )}
        <section aria-labelledby="ownership">
          <h2 id="ownership">Your content</h2>
          <p>You keep ownership of what you upload and what you generate, subject to the licences of the models used. Motions, movies and scene flights generated on GPUs use MiniMax H3; character image generation uses FLUX.2 Klein (Apache-2.0); motions you send to Higgsfield use Higgsfield&rsquo;s models under Higgsfield&rsquo;s terms.</p>
          <p>You grant Boundless Games, LLC a licence to host, process and transmit your content only to operate the service for you.</p>
        </section>
        <section aria-labelledby="responsibilities">
          <h2 id="responsibilities">Your responsibilities</h2>
          <p>You must have the rights to what you upload. You are responsible for any machine you attach, and for your third-party accounts and keys, including your Higgsfield account.</p>
        </section>
        <section aria-labelledby="acceptable-use">
          <h2 id="acceptable-use">Acceptable use</h2>
          <p>You may not use the studio to make or attempt the following. Prompts are screened automatically against rules 1 to 6 before any generation runs, and we enforce all of them:</p>
          <ol>
            <li>Any sexual content involving minors, or content that sexualises minors. This is always refused. We report apparent child sexual abuse material to NCMEC as US law requires, and we terminate the account.</li>
            <li>Sexually explicit content of any kind.</li>
            <li>Content depicting real, identifiable people in sexual, intimate or degrading situations, or deceptive impersonation of real people, such as deepfakes meant to mislead.</li>
            <li>Content that threatens, harasses or targets real people, or attacks people based on protected characteristics.</li>
            <li>Content that promotes or glorifies terrorism, violent extremism or real-world mass violence, or gives instructions for serious harm, such as weapons capable of mass casualties.</li>
            <li>Content that encourages suicide or self-harm.</li>
            <li>Anything illegal, anything that infringes others&rsquo; rights, or abuse of the service, such as circumventing limits or screening, attacking or overloading it, or sharing accounts to evade enforcement.</li>
          </ol>
          <p>Fictional violence, combat and gore in games and stories, horror, villains, and dark and mature themes are allowed when they do not cross the lines above.</p>
          <p>A refused prompt returns an error naming the rule. Repeated or severe violations can lead to suspension or termination. Report abuse to {contact}.</p>
        </section>
        <section aria-labelledby="termination">
          <h2 id="termination">Ending your use</h2>
          <p>You can delete your account at any time from the account menu. We may suspend or terminate accounts that violate these terms.</p>
        </section>
        <section aria-labelledby="warranties">
          <h2 id="warranties">No warranties</h2>
          <p>To the extent the law allows, the service is provided without warranties of any kind, express or implied, including fitness for a particular purpose and non-infringement. We do not promise that it will be uninterrupted or error-free, or that generated results will meet your needs.</p>
        </section>
        <section aria-labelledby="liability">
          <h2 id="liability">Limitation of liability</h2>
          <p>To the extent the law allows, Boundless Games, LLC is not liable for indirect, incidental, special, consequential or punitive damages, or for lost profits, data or content, arising from your use of the service.</p>
        </section>
        <section aria-labelledby="indemnity">
          <h2 id="indemnity">Indemnity</h2>
          <p>You agree to defend and compensate Boundless Games, LLC for claims and costs arising from your content, your use of the service, or your breach of these terms.</p>
        </section>
        <section aria-labelledby="law">
          <h2 id="law">Governing law</h2>
          <p>These terms are governed by the laws of the State of Delaware and the federal laws of the United States, without regard to conflict-of-law rules.</p>
        </section>
        <section aria-labelledby="terms-changes">
          <h2 id="terms-changes">Changes to these terms</h2>
          <p>When we change these terms, we update the date on this page. For material changes, we tell signed-in users in the studio before the changes apply.</p>
        </section>
        <section aria-labelledby="terms-contact">
          <h2 id="terms-contact">Contact</h2>
          <p>For questions about these terms, legal notices and abuse reports, email {contact}.</p>
        </section>
      </main>
      <PublicFooter />
    </>
  )
}
