# Magic Studios agent guide

Magic Studios is the `magic-video-editor` service and React web application.
Read the relevant sections of `README.md` for service boundaries, local setup,
provider contracts, and deployment. Backend code lives in `src/magic_video_editor`,
backend tests in `tests`, frontend code in `web`, and infrastructure in `infra`.

## Repository checks and boundaries

Use `uv run --extra test pytest -q tests/<affected_test>.py` for focused backend
checks. PostgreSQL integration tests need `MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL`;
report skipped coverage when a database is unavailable. Full CI also needs ffmpeg.
For frontend changes, run the applicable Vitest tests from `web` with
`npm test -- --run`, plus `npm run lint` and `npm run build`. Use the relevant
Playwright flow for browser behavior changes; CI's main flow is
`npm run e2e -- studio-flow.spec.ts`. See `.github/workflows/ci.yml` for full gates.
Documentation/configuration-only changes need syntax, links, and diff checks.

Preserve immutable publication, fenced job completion, provider attestation,
and verified actor identity boundaries described in the README. Keep unrelated
working-tree changes intact.

## Hosted machine profiles

- Use the pinned pool package's explicit `machine_profiles`; never infer a recipe
  from VRAM alone. Its 16GB graph is opt-in and the standard 24GB graph/minimum
  remain separate. Enrollment verifies preinstalled models/nodes/runtime before
  spending a pairing code; it does not provision weights or attention wheels.
- Hosted discovery is scoped by `_tenant_for`, including existing admin-cluster
  rules. `worker_views(None)` means all tenants in the library: filter house rows
  explicitly. Caller affinity must never widen tenant visibility/eligibility.
- Resolve settings before dispatch; preserve explicit overrides or reject them.
  Persist worker ID plus recipe/configuration requirements, and enforce both in
  memory/PostgreSQL leasing and completion attestation. Do not reintroduce a
  Cloud Tasks fallback for bound jobs. A failed/offline pinned worker needs a new
  resolution to move work; never silently switch profiles.
- Verify wheel contents and both coordinator/worker dependency pins when changing
  recipes. Use `scripts/verify_machine_profile.py` only with idle GPU queues; it
  uses temporary protocol state and must not overwrite selected Studio assets.
  Local protocol success is not evidence of hosted pairing or a 24GB benchmark.

- Enrollment acceptance includes restart recovery, not just pairing: verify the
  worker OS can find ffmpeg/ffprobe, ComfyUI can start independently when absent,
  and WSL itself starts at Windows sign-in if used. Preserve launch flags and
  profile; never restart a busy runtime. Existing workers need the documented
  credential-preserving script update; server deployment does not update them.
