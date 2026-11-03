import { readdirSync } from 'node:fs'

import { sentryVitePlugin } from '@sentry/vite-plugin'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import type { Plugin, PluginOption } from 'vite'
import { defineConfig } from 'vitest/config'

// Source-map upload only runs when the full credential triple is present at
// build time. It is never required locally or in CI test/build steps — only
// the production deploy step supplies these, from repo secrets, never from
// source.
const sentrySourceMapUploadPlugins: PluginOption[] =
  process.env.SENTRY_AUTH_TOKEN && process.env.SENTRY_ORG && process.env.SENTRY_PROJECT
    ? [
        sentryVitePlugin({
          authToken: process.env.SENTRY_AUTH_TOKEN,
          org: process.env.SENTRY_ORG,
          project: process.env.SENTRY_PROJECT,
          release: { name: process.env.VITE_APP_RELEASE },
        }),
      ]
    : []

// E2E only: the dev server compiles a module the first time a browser asks for it, so a
// spec's first visit to a lazy route timed the compile, not the app (the 2000 ms cached
// navigation budget saw 3000+ ms on CI). Vite's own `server.warmup` is not awaited, so
// specs raced it. Playwright instead waits on `/__e2e_ready`, which answers only once the
// entry and every lazy route, with everything they import, are compiled.
const E2E_ENTRIES = [
  '/src/main.tsx',
  '/src/features/project/ProjectRoute.tsx',
  '/src/features/subject/SubjectWorkspace.tsx',
  '/src/features/motions/MotionWorkspace.tsx',
  '/src/features/release/ReleaseWorkspace.tsx',
  '/src/features/project/ProjectHistory.tsx',
  '/src/features/flights/FlightRoute.tsx',
  '/src/features/movies/MovieRoute.tsx',
  ...readdirSync(new URL('./src/features/auth', import.meta.url))
    .filter((name) => name.endsWith('Page.tsx'))
    .map((name) => `/src/features/auth/${name}`),
]

function e2eReady(): Plugin {
  return {
    name: 'e2e-ready',
    apply: 'serve',
    configureServer(server) {
      const client = server.environments.client
      // Only what a browser loads: app source and the pre-bundled dependencies it imports.
      const loaded = /^\/(src\/|node_modules\/\.vite\/deps\/)/
      const compile = async (url: string, seen: Set<string>): Promise<void> => {
        if (seen.has(url) || !loaded.test(url)) return
        seen.add(url)
        // A module that fails to compile fails in the browser too; the spec reports it.
        await client.transformRequest(url).catch(() => undefined)
        const node = await client.moduleGraph.getModuleByUrl(url)
        await Promise.all(Array.from(node?.importedModules ?? [], (child) => compile(child.url, seen)))
      }
      let ready: Promise<void> | undefined
      server.middlewares.use('/__e2e_ready', (_request, response) => {
        const seen = new Set<string>()
        ready ??= Promise.all(E2E_ENTRIES.map((url) => compile(url, seen))).then(() => undefined)
        ready.then(
          () => { response.statusCode = 200; response.end('ready') },
          (error: unknown) => { ready = undefined; response.statusCode = 503; response.end(String(error)) },
        )
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), ...sentrySourceMapUploadPlugins, ...(process.env.STUDIO_E2E_WARMUP ? [e2eReady()] : [])],
  build: {
    // The Sentry plugin only picks up source maps that Vite actually emits.
    sourcemap: sentrySourceMapUploadPlugins.length > 0,
  },
  server: process.env.STUDIO_API_ORIGIN ? {
    proxy: {
      '/studio': { target: process.env.STUDIO_API_ORIGIN, headers: { 'X-Studio-Actor-Id': process.env.STUDIO_DEV_ACTOR_ID ?? 'local-studio-developer' } },
      '/pool': { target: process.env.STUDIO_API_ORIGIN, headers: { 'X-Studio-Actor-Id': process.env.STUDIO_DEV_ACTOR_ID ?? 'local-studio-developer' } },
      '/local-assets': { target: process.env.STUDIO_API_ORIGIN },
      // E2E-only introspection of what the local stack's recording target adapter
      // received, so a spec can assert a release actually delivered without a
      // second live service.
      '/local-target-deliveries': { target: process.env.STUDIO_API_ORIGIN },
    },
  } : undefined,
  test: {
    environment: 'jsdom',
    exclude: ['e2e/**', 'node_modules/**'],
    setupFiles: './src/test/setup.ts',
    // The tests cover the hosted service's pages; hostedService.test.tsx covers a
    // self-hosted build, where they are left out.
    env: { VITE_HOSTED_SERVICE: 'true' },
  },
})
