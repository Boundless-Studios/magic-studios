# Movies Implementation Plan

Goal: deliver a durable movie workspace backed by dependency-aware GPU generation.
Architecture: separate revisioned movie domain, existing worker pool, CPU assembly,
React workspaces sharing the current studio shell.
Tech stack: FastAPI, Pydantic, PostgreSQL, outbound GPU pool, FFmpeg, React, Vitest.

1. Backend domain and API (`src/magic_video_editor/movies/`, movie migration,
   runtime wiring, `tests/movies/`). Write failing scheduling/ownership tests;
   implement revisioned stores, bounded fan-out, composition review, take lineage,
   CPU extraction/assembly, release and authenticated asset grants. Verify with
   `PYTHONPATH=src .venv/bin/python -m pytest tests/movies -q`.
2. Movie workspaces (`web/src/features/movies/`, App onboarding and routing,
   shared shell styling). Write failing user-flow tests, implement Story → Shots
   → Cut → Release with explicit generation budget and history. Verify using
   `npm test -- --run` and `npm run build` from web.
3. Integration: run the local app, exercise the workflow with fixture media,
   inspect the UI at desktop/mobile sizes, and run backend regressions.
4. Independent spec review followed by code-quality review; fix findings and
   repeat affected tests. Commit the reviewed implementation on codex/movies.

Execution uses the subagent-driven-development skill: one backend implementer
while the primary agent handles the independent frontend integration, followed by
independent review. The approved user request authorizes continued implementation.

## Completed verification

- Backend: 691 tests passed, including a disposable real PostgreSQL instance.
- Web: 167 tests passed; ESLint and the production build passed.
- Browser: all six movie/animation flow checks passed.
- Independent spec and code reviews approved after fixes for obsolete release
  cuts, JSON digest stability, draft concurrency, and ambiguous-submit retries.
- Opening-review and audio MIME regressions passed. Local proof is saved under
  `/tmp/magic-movies-proof`; fixture generation made no external GPU calls.
- Production deployment, hosted-planner provisioning and a live GPU render have
  not been performed. The worktree remains on `codex/movies` for review.
