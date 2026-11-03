# Contributing to Magic Studios

Thanks for helping. Bug reports, fixes, docs and new agent tools are all
welcome.

## How this repository works

Magic Studios is developed in an internal repository that also holds the hosted
service's deployment. This public repository is a snapshot exported from it, and
updates arrive as sync commits.

You can still open issues and pull requests here as usual. When a maintainer
accepts a pull request, they apply it to the internal repository with your
authorship kept, and it comes back here in the next sync. Your pull request is
then closed with a link to the sync commit rather than merged directly.

By contributing, you agree that your contribution is licensed under this
repository's MIT License.

## Before you start

- **Security problems:** do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **Larger changes:** open an issue first so we can agree on the approach. This
  matters most for anything that touches identity, tenant isolation, job
  leasing and completion, or published releases. See *Boundaries* below.
- **Model weights and media:** never commit model weights, generated media,
  credentials or runtime state. Each model has its own licence; see
  [NOTICE](NOTICE).

## Set up

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22 or newer,
and FFmpeg on your `PATH`. The PostgreSQL integration tests also need a
PostgreSQL 16 database.

```bash
uv sync --extra test
cd web && npm ci
```

`magic_video_editor.local_studio:app` runs the real routes, stores and worker
code with an in-memory store and a deterministic stand-in GPU, so no GPU or
cloud account is needed. The Playwright suite starts it for you (see the README's
*Local development* section).

## Checks

Run the checks for what you changed:

```bash
# Backend: a focused test file, then the whole suite
uv run --extra test pytest -q tests/<affected_test>.py
uv run --extra test pytest -q

# PostgreSQL integration tests (skipped without this variable)
MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5432/magic_video_editor \
  uv run --extra test pytest -q tests/test_postgres_integration.py

# MCP server
uv pip install -e "./mcp[test]" && uv run pytest -q mcp/tests

# Web app, from web/
npm test -- --run
npm run lint
npm run build
npm run e2e -- studio-flow.spec.ts
```

Add a regression test for each bug you fix and for each meaningful way your
change can fail. Say in the pull request which checks you ran, and what you
could not run: for example, PostgreSQL tests without a database, or anything
that needs a real GPU worker.

## Boundaries

Keep these intact. A change that weakens one needs an explicit discussion first:

- **Verified actor identity.** Routes act only for a verified actor, API key
  or enrolled machine.
- **Tenant isolation.** Worker discovery and leasing stay scoped to the owner's
  tenant, and a caller's preference never widens it.
- **Fenced job completion and provider attestation.** Only the current lease
  holder completes a job, and it attests what it actually ran.
- **Immutable publication.** Releases, exports and revisions never change after
  they are published.
- **Honest settings.** Resolve generation settings before dispatch and record
  what actually ran. Never relabel a job, for example as 20 steps when it ran 8.

## Pull requests

- Keep each pull request to one change, and describe the behaviour it changes
  and why.
- Match the surrounding code's style, naming and comment density.
- Do not reformat or rename code you did not otherwise need to touch.
- Keep the web app usable on a phone-width screen and by keyboard.
