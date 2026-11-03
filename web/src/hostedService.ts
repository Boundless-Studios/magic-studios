/**
 * Pages only the hosted magicstudios.ai service serves.
 *
 * The hosted service runs Studio with its private `hosted` extension, which adds
 * tips (`/studio/donations/*`) and the admin review of past access requests. Its
 * deploy builds the web app with `VITE_HOSTED_SERVICE=true`. A self-hosted build
 * leaves it unset, so the Support link, the tip pages, the tips sections of the
 * about page and the terms, and the past-requests admin page are left out rather
 * than pointing at routes its server does not have.
 */
export const hostedService = import.meta.env.VITE_HOSTED_SERVICE === 'true'
