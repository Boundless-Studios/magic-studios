/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SENTRY_DSN?: string
  readonly VITE_SENTRY_ENVIRONMENT?: string
  readonly VITE_APP_RELEASE?: string
  readonly VITE_CF_BEACON_TOKEN?: string
  /** Prefix of the staged site media, e.g. `/assets/site-media/1`; unset in public builds. */
  readonly VITE_SITE_MEDIA_BASE?: string
  readonly VITE_HOSTED_SERVICE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
