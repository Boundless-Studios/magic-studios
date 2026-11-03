# Magic Video Editor

Standalone, scale-to-zero video generation and packaging service for Magic Studios.

Cloud Tasks invokes one job per request; the service scales to zero between jobs.
Database and asset access are supplied by the PostgreSQL and GCS plugins through
environment configuration and Secret Manager bindings.

The web app includes a separate **Movie Studio** for shot planning, distributed
clip generation, take review, stitching and soundtrack assembly. See
[the Movie Studio guide](docs/movies.md) for the workflow and runtime requirements.

**Scene flights** fly the camera between location images: each leg is one MiniMax H3
first+last-frame clip that starts exactly on its origin image and ends on its
destination image, so chained legs read as one continuous move. Flights reuse the
Movie Studio pipeline at 768×448, 24 fps, 20 steps, 5–15 s per leg, through
`/studio/scene-flights`, the `*_scene_flight*` MCP tools and **New project → Scene
flight** in the web app. The desktop presets and the pinned pool's standard template
both bind the last frame. See [the scene-flight guide](docs/scene-flights.md).

**Audio sets** gather sound effects, ambience, music and voice lines per cue from
Freesound (CC0 by default, CC-BY as a per-set opt-in) and the owner's own uploads, for
review, selection and an immutable release zip with `manifest.json` and `CREDITS.md`.
They live under `/studio/audio`, the `*audio*` MCP tools and **New project → Audio**.
Free-sound search needs the optional `FREESOUND_API_KEY` repository secret (mounted
as `MAGIC_VIDEO_EDITOR_FREESOUND_API_KEY`); without it audio sets take uploads only.
Invited accounts' candidates get a loudness rendition and measurements on the
service's FFmpeg; public accounts keep originals only. Audio sets are not yet part of
inactivity expiry, and nothing is generated yet. See
[the audio design](docs/design/audio-studio.md).

## Machine defaults: 16GB desktop and standard 24GB workers

Generation uses the **configured machine's default**, not a public A/B/C quality menu.
VRAM size alone does not select a recipe or prove compatibility. The desktop's
`balanced` configuration is separate from the standard hosted worker configuration;
production routing is not lowered to fit a smaller card.

| Setting | This RTX 4080 16GB desktop (configured default B) | Standard 24GB motion-worker path (existing default) |
| --- | --- | --- |
| H3 diffusion weights | `minimax_h3_fl2va_pruned_w4a8_mixed.safetensors`: mixed **4-bit weights / 8-bit activations** | `minimax_h3_fl2va_pruned_int8_convrot.safetensors`: pruned **INT8 ConvRot** |
| Text encoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`: **4-bit NVFP4/AWQ** | Same NVFP4/AWQ encoder in the pinned worker template |
| Video VAE | `minimax_h3_video_vae_int8_convrot.safetensors`: **INT8 ConvRot** | `minimax_h3_video_vae_fp16.safetensors`: **FP16** |
| Turbo adapter | **LightX2V FL2V 8-step v1.0 768p**, strength **1.0** (`minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors`) | **None** |
| Sampling | **8 steps**, `res_multistep` / `simple` | **20 steps**, `res_multistep` / `simple` for sprite motions |
| H3 attention | Per-model **Comfy Kitchen** override | **Worker's ComfyUI attention setting**; the standard graph does not force Kitchen or Sage |
| Process-wide attention | **SageAttention 2.2**; H3 itself uses the Kitchen override above | Not forced by this repository; inspect the worker launch configuration. Do not assume Sage/Kitchen from “24GB” |
| Motion request size/time | **512×512**, **4 requested seconds**, seed **42**, chroma green | Existing animation request defaults: **512×512**, **4 seconds**, seed **42**, chroma green; larger supported requests remain available |
| Audio | H3 joint latent is sampled; **audio VAE decode and export omitted** | Standard worker graph retains **FP32 audio VAE** and audio decode; separate movie/dialogue defaults are unchanged |
| Memory/runtime | Native Windows portable ComfyUI **0.41.0**, PyTorch **2.14.0+cu130**; dynamic VRAM, fast disk, pinned memory disabled, **0GB reserved**; tiled video decode | Worker-managed runtime/offload/reservation settings; no 4080-specific override is imposed |
| Routing | `video.minimax_h3.text_to_video.w4a8_16gb.lightx8.v1`, minimum **16,000 MB** | `video.minimax_h3.text_to_video.v1`, minimum **24,000 MB** |
| Validation | Tested on this **4080 16GB** at up to 512×512/four seconds; B requires exactly 8 steps | Existing worker contract, **not a new 4090 benchmark or quality ranking** |

The 24GB model filenames above come from the pinned `outbound-gpu-worker-pool`
revision `4a6c55a` templates, not from the modified desktop templates in `local/`.
A worker with custom templates/flags can differ; its actual configuration remains
authoritative. The table concerns **sprite FL2VA motions**, not the Ref2VA dialogue
pipeline. More VRAM does not automatically require different sampling settings,
but B's quality/timing results must not be claimed for an untested 24GB card.

On this desktop, `MAGIC_DESKTOP_PRESET=balanced` resolves omitted sprite settings
to B **before** recording job provenance. The UI is configured with
`VITE_MAGIC_DESKTOP_RECIPE=balanced`; it displays 8 steps and exposes no quality-tier
selector. `GET /studio/generation-config`, also exposed as MCP
`get_generation_config`, reports the active desktop default. The desktop resolver
is not enabled in production. Standard deployments continue to use their existing
24GB route; a hosted coordinator must explicitly support the separate 16GB
capability before it can schedule this machine. Local success is not enrollment.
Hosted integration now supports explicit worker profiles, tenant-scoped discovery,
pre-dispatch machine defaults and recipe-bound leases. See the [hosted setup
runbook](docs/hosted-machine-setup.md) and [acceptance checklist](docs/plans/hosted-machine-defaults-follow-up.md).
Deployment and live pairing must be verified separately. Legacy workers retain their
existing standard path until migrated to an explicit verified profile.

For this already provisioned desktop, switch only while the GPU queue is idle:

```bash
local/preset.sh balanced ck windows
local/secure-local-access.sh
local/start.sh
```

The desktop server needs this checkout's `local/presets` and helper scripts even
when its Python package is installed from a wheel. Start it from the checkout,
or set `MAGIC_DESKTOP_ROOT` to the checkout's absolute path when launching from
another directory. Editable Python installs discover the source checkout
automatically. This path is independent of the ComfyUI installation: the server
connects through `MAGIC_DESKTOP_COMFY_URL`, including Windows portable ComfyUI.

The preset helper sets global Sage separately from H3 Kitchen, matching the measured
recipe. Other developer presets are internal experiments, **not supported quality
tiers**. The legacy `turbo` preset uses a partial Larry conversion and is not B.
Keep its limitations explicit; do not select it as an alias for the winning recipe.

Native FFmpeg frame decoding/caching, loop suggestions, fixed body normalization,
canvas expansion, exported pivots and MCP editing apply to both hardware variants.
Quantization, attention, offload, reserved VRAM and workload bounds belong to each
machine. Zero reservation is appropriate for this desktop's dedicated compute GPU,
not an unconditional default for a card also driving the display.

### Use the desktop from a Mac

Local UI/MCP listeners bind to **127.0.0.1**, with no MCP token needed behind the
user's authenticated SSH tunnel. They are not unauthenticated LAN services.
Forward **both** UI/assets and MCP ports from the Mac using its existing SSH alias:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:5173:127.0.0.1:5173 \
  -L 127.0.0.1:8766:127.0.0.1:8766 desktop
```

Then connect Streamable HTTP MCP at `http://127.0.0.1:8766/mcp` and browse
`http://127.0.0.1:5173`. `desktop` is your working Mac SSH alias. Remote-loopback
reachability must be verified from that SSH endpoint; Mac-to-desktop forwarding
is not proven by an on-desktop test. See the [Mac setup and read-only test prompt](.agents/skills/magic-studios/references/connection.md).

The checked-in [Magic Studios skill](.agents/skills/magic-studios/SKILL.md) teaches
agents remote uploads, generation using machine defaults, walk/attack extraction,
scale/pivot preservation, inspection and downloads. Copy its folder to the client; the
web app no longer serves it as a zip. Outside users get the separate public skill
(`mcp/skill/`) and the MCP server from `/studio/downloads`. Start with a read-only
connection test, then use a new
project for a real generation test. No models or ComfyUI are required on the Mac.

See [the final experiment record](docs/h3-walk-experiments.md) for timings and the
user-selected B output.
These findings are specific to the tested walk;
attack quality and other directions still require their own review.

## Per-owner limits

Two caps keep one operator from consuming the shared bucket or the whole GPU
pool. Both refuse only the *start* of new work: approving, releasing, exporting
and downloading what already exists are never blocked. `0` means unlimited.

| Variable | Default | Effect when reached |
| --- | --- | --- |
| `MAGIC_VIDEO_EDITOR_OWNER_STORAGE_LIMIT_BYTES` | `53687091200` (50 GiB) | New uploads and generations answer `507` with `owner_storage_limit_reached` |
| `MAGIC_VIDEO_EDITOR_OWNER_CONCURRENT_JOB_LIMIT` | `8` | New generations answer `429` with `owner_concurrent_job_limit_reached` |

Stored bytes are tracked incrementally in `studio_owner_usage` as artifacts are
settled, never by listing the bucket. `GET /studio/auth/session` returns the
signed-in operator's own usage and limits under `usage`.

## Motion-operation rollout

Versioned motion jobs are fail-closed behind
`MAGIC_VIDEO_EDITOR_MOTION_OPERATIONS_ENABLED`, which defaults to `false` in
both API and worker processes. Deploy in this order:

1. deploy the compatible image to both API and worker services with the shared
   flag disabled;
2. verify both services are healthy and validate the configured GPU provider
   against the immutable-idempotency v1 response contract;
3. set the shared flag to `true`, redeploy both services, and verify health now
   advertises `generate_motion_v1` before accepting production traffic.

During rollback, stop new motion submissions at the trusted gateway, drain or
cancel queued jobs, then disable the shared flag and redeploy both services.
The trusted gateway must strip caller-provided `X-Studio-Actor-Id` and inject
the verified actor identity, matching the Studio route security boundary.

The browser application never sends `X-Studio-Actor-Id`. Production must expose
Studio routes through a same-origin authenticated gateway/BFF that removes any
incoming copy and injects the verified identity server-side. For local frontend
development only, set `STUDIO_API_ORIGIN`; Vite's development proxy injects
`STUDIO_DEV_ACTOR_ID` (default `local-studio-developer`) on the server side.
That development identity mechanism must not be used in a production build.

GPU providers must treat `idempotency_key` as a create-once generation key.
Replays return the original immutable `output_key`; they must not rerun or
overwrite generation. This is required because a database completion can fail
after the provider has successfully created the artifact. The HTTP adapter
fails closed unless each success attests the supported contract version, exact
canonical request digest, matching idempotency key, and create-once publication.
The fenced database completion is the publication boundary for the project: an
artifact produced by a stale or cancelled claim is never exposed as a completed
job, though the provider may retain it as an unreferenced immutable artifact.

The service does not claim exactly-once GPU billing. A provider can charge for
work that finishes after a timeout or cancellation; commercial deduplication
and billing guarantees must be established with the configured provider.

## Subject-operation provider

`MAGIC_VIDEO_EDITOR_SUBJECT_PLUGIN` selects how the first Studio step,
`GENERATE_SUBJECT`, is served: `none` (the fail-closed default) leaves
`subject_generation_available` `False`; `http` points at an external
`HttpSubjectGenerator` endpoint via `MAGIC_VIDEO_EDITOR_SUBJECT_ENDPOINT`/
`MAGIC_VIDEO_EDITOR_SUBJECT_AUDIENCE`; `pool` (the production value) routes
subject generation through the same outbound GPU worker pool as motions —
`PoolSubjectGenerator` submits an `image.flux2_klein.subject.v2` job for a
fixed studio prompt built around the caller's direction, binding up to three
of the caller's uploaded reference images to the capability's image slots so
the generated subject matches the reference, polls it to completion, keys the
raw PNG the pool wrote against the green screen backdrop it estimates from
the image's own border, and stores the canonical RGBA subject artifact.

On an owner's own machines the route follows what those machines advertise. A machine
serving `image.flux2_klein.subject.v2` (16,000 MB) keeps that route. A machine attached
with the **Subjects 12GB** configuration (pool profile `flux2-klein-subject-12gb`) serves
`image.flux2_klein.subject.12gb.v1`: the same graph at 1024², exactly 20 steps and at most
three references, with a 12,000 MB minimum. That route was measured on an RTX 4090 with VRAM
held to 11,914 MiB free, not on a physical 12 GB card; two references were not in the
matrix.

Only machines that are still enrolled and online now (heard from within 90 s) decide the
route, so an offline or removed 16 GB machine never pulls work off an online 12 GB one. A job
already in the pool is polled as submitted, without choosing a route again. When none of the
owner's machines can serve either route, the subject (or a movie opening frame) is refused
before it queues, with `machine_cannot_run` and a per-machine reason such as "subject images
need a 12 GB card and this one has 8 GB". House work keeps the 16 GB route. The 16,000 and 24,000 MB minimums are unchanged.

**Smaller cards degrade instead of failing silently.**
- **Machines page and `/pool/workers`.** Each machine row carries `features`: subject images,
  motions, movie takes and movie processing. Each one says whether the machine can run it, and
  if not, why. For example: "motions need a 16 GB card and this one has 12 GB". A feature is
  available only when the machine advertises the capability and reports the VRAM it needs.
  VRAM only explains a refusal; it never selects a recipe.
- **Motions.** Discovery gives the same per-machine reasons, and so does the 422 when no
  machine fits. That 422 also points to the Higgsfield key, which stays offered for motions.
- **Movies.** Takes, auditions, openings and portraits that none of the owner's machines can
  run are refused before anything queues (`machine_cannot_run`).
- **Subjects.** If none of the owner's machines picks up a subject within the 900 s poll, it
  is cancelled with that reason rather than retried for over an hour.
- **Attaching a card that is too small.** If the card is below its chosen profile's minimum,
  the machine attaches without that profile for what it can still run, such as CPU movie
  processing, and reports the card. Missing models, nodes or flags still stop the attach
  before the pairing code is spent, and no other profile is chosen for it.

## Composition-operation rollout

Opening-frame `COMPOSE_FRAME` jobs (T15) are fail-closed behind
`MAGIC_VIDEO_EDITOR_COMPOSITION_OPERATIONS_ENABLED`, which defaults to `false`.
Set `MAGIC_VIDEO_EDITOR_COMPOSITION_PLUGIN=http` (default `none`) plus
`MAGIC_VIDEO_EDITOR_COMPOSITION_URL` and `MAGIC_VIDEO_EDITOR_COMPOSITION_AUDIENCE`
to point the worker at a production composition provider; the audience/identity
mechanism matches the subject provider's Google-signed ID token flow. With the
plugin set to `none`, or the operations flag left `false`, the service's
`composition_generation_available` stays `False` and `/health` does not
advertise `compose_frame_v1`.

The `HttpCompositionGenerator` adapter mirrors the subject and GPU HTTP
adapters: it sends `Idempotency-Key` and `X-Composition-Contract-Version:
composition-immutable-v1` headers, verifies the response attests a matching
output key, idempotency key, canonical request digest, contract version, and
`publication_mode == immutable_create_once`, and fails closed otherwise. GPU
and subject 429/5xx/timeout failures are retryable; other 4xx failures and
attestation mismatches are terminal. Requests run inside one wall-clock budget
with a short, separately bounded identity-token timeout, and error bodies are
truncated before they reach logs or job failure messages.

## Outbound GPU worker pool

GPU workers pull work and the service never calls a worker. That whole mechanism —
the durable capability lease, the authenticated `/worker/v1` surface, the
job-scoped asset grants, the verified create-once publication, and the worker
agent — is the
[`outbound-gpu-worker-pool`](https://github.com/Boundless-Studios/outbound-gpu-worker-pool)
library. This service owns none of it: it configures the library's coordinator,
mounts the library's worker router, and submits jobs through its own
authenticated routes. Pool state lives in the library's `pool_jobs`,
`pool_workers`, and `pool_audit_events` tables in the same database; `video_jobs`
remains the ledger for the product's own Cloud Tasks operations.

### Coordinator configuration

| Variable | Values | Meaning |
|---|---|---|
| `MAGIC_VIDEO_EDITOR_WORKER_AUTH` | `none` (default), `static`, `google_oidc` | `none` leaves the pool unconfigured and `/worker/v1` unmounted |
| `MAGIC_VIDEO_EDITOR_WORKER_TOKENS` | `worker-id:<sha256 hex>[,...]` | required for `static`; only digests are stored |
| `MAGIC_VIDEO_EDITOR_WORKER_AUDIENCE` | audience string | required for `google_oidc` |
| `MAGIC_VIDEO_EDITOR_SERVICE_ROLE` | `coordinator` | mounts only `/health` and `/worker/v1/*` |
| `MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS` | JSON object, default empty | operator-approved workers that have no registry row yet: `{"<worker-id>": {"identity_subject": "<full subject>", "tenant_id": null}}`; the tenant key is mandatory and `null` means the house pool |
| `MAGIC_VIDEO_EDITOR_WORKER_PRE_AUTH_LIMIT_PER_MINUTE`, `..._PER_SOURCE_LIMIT_PER_MINUTE`, `..._AUTH_AUDIT_LIMIT_PER_MINUTE`, `..._MAX_AUTH_CONCURRENCY` | positive integers | per-process admission limits before worker authentication; unset keeps the library defaults (1,200 total, 120 per source, 30 sampled audit writes, 16 concurrent) |

A worker authenticates only with an existing `pool_workers` binding or an
explicit server-side enrollment; neither a first heartbeat nor an account name
chooses its identity or tenant. A machine attached through Studio is enrolled
when its pairing code is redeemed, under the owner who minted the code. Other
workers are listed in `MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS`. Existing registry
rows keep working unchanged. Per-source admission keys on the ASGI peer
address; machines behind one proxy share a budget, and forwarded headers are
never trusted for it.

The pool's asset store is this service's `GcsAssetStore`: read grants are limited
to `sources/pool/` and `outputs/pool/`, and upload grants are create-once under
`outputs/`.

### Pool job routes

Submission is the product's own authenticated surface, served for the `all` and
`api` roles. Each route requires a signed-in caller (session or API key), and job
submission is refused for public accounts, since its jobs run untenanted on the
house machines:

- `POST /pool/jobs` — `idempotency_key`, `capability_id`, `input_keys` (up to 16
  normalized keys under `sources/pool/`), `output_key` (under `outputs/pool/`),
  `payload`, and the optional `priority`, `attempt_budget`, and
  `execution_deadline_seconds`. An equal replay returns the same job; a reused
  idempotency key for a different request is `409`. `input_keys` may be empty:
  a text-only capability such as `video.minimax_h3.text_to_video.v1` takes all
  of its request from the `payload`.
- `GET /pool/jobs/{job_id}` — status, attempts, failure, and the attested output
  digest and byte length. The lease claim token is never part of a response.
- `POST /pool/jobs/{job_id}/cancel` — cancels a queued or processing job.

All three answer `503` while `MAGIC_VIDEO_EDITOR_WORKER_AUTH` is `none`.

The agent protocol itself, the plugin contract, and the failure semantics are
documented in the library's README.

### The coordinator service

Workers are outbound: they live on machines this project cannot call, so they
must be able to call in. The Studio API is behind IAP and the execution worker is
internal-only, so neither can serve them. `magic-video-editor-coordinator` is a
third Cloud Run service running the same image with
`MAGIC_VIDEO_EDITOR_SERVICE_ROLE=coordinator` and
`MAGIC_VIDEO_EDITOR_WORKER_AUTH=google_oidc`. It publishes `/health` and
`/worker/v1/*` and nothing else — no Studio routes, no Cloud Tasks target, no GPU
plugin, and no Cloud Tasks enqueue permission.

Its ingress is `INGRESS_TRAFFIC_ALL` because a worker on someone's desk has no
VPC route in, but it grants no `allUsers` invoker: every caller must be an IAM
identity holding `roles/run.invoker` on this service. That grant decides who may
reach the service, not who joins the pool: a verified `gpu-worker-<id>@...`
identity with no `pool_workers` row is admitted only when
`MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS` names that exact account, and the entry,
not the account name, fixes its worker id and tenant. Its first heartbeat then
writes the row, which is what a later `drain` or `revoke` acts on. Read the
service URL, which is also the audience workers sign for, from Terraform:

```bash
tofu -chdir=infra output -raw coordinator_url
```

The audience is that same URL. Reading it back from the coordinator resource
would make the service depend on itself, so `coordinator_audience` defaults to
the deterministic Cloud Run URI built from the project number and region — the
value the service is about to get. One apply configures both sides, and
`tofu -chdir=infra output -raw coordinator_audience` is what a worker signs for.
Override the variable only when the coordinator is reached through a custom
domain, whose audience Terraform cannot derive.

### Attaching an admin's machine through Studio

An invited admin signs in, opens the machines page, and copies its one-command
setup for PowerShell or Linux. The command uses a one-hour pairing code to
download the worker package from Studio, verifies its SHA-256 digest, installs
the worker with its agent/comfy dependencies, then redeems the code to attach
the machine. Git and GitHub credentials are not required on the machine. uv,
Python, and public Python dependencies still require internet access.

The Docker build creates the universal wheel from the worker dependency pinned
in `pyproject.toml`, includes it and its digest in the application package, and
fails if it cannot build it. The build environment needs access to that source
repository. The existing invite-only sign-in and pairing-token controls remain
the access boundary; this does not add a separate admin role or change GPU job
routing.

`POST /studio/workers/package` accepts the pairing token in a JSON body and
returns the wheel only for an unspent, unexpired code. Downloads are not cached
and do not spend the code. A failed installation can therefore be retried with
the same command until the code expires. Dependency installation diagnostics
are saved under `~/.magic-video-worker/install.log` on Linux or
`%USERPROFILE%\.magic-video-worker\install.log` on Windows. No pairing code is
passed to the package installer or written to that log.

For a source-based development server, first run
`uv run --with pip python scripts/build_worker_package.py`. Alternatively,
`MAGIC_VIDEO_EDITOR_WORKER_PACKAGE_DIR` can point to a private directory holding
the built wheel and `manifest.json`. A missing or damaged package returns 503;
the installer never falls back to a public source download.

### Enrolling an outbound machine

Enrolment is an operator command, not a deploy. No machine is named in source, in
Terraform, or in CI settings: GCP IAM is the roster and the `pool_workers`
registry is that roster's state, and `scripts/pool_admin.py` is what writes
both.

Enrolment needs only `gcloud`, authenticated as a principal that may create
service accounts and edit the coordinator's Cloud Run IAM policy. No Cloud SQL
Auth Proxy and no database. The invoker grant lets the machine reach the
coordinator; the pool admits it only once the `enrollment` entry the command
prints is in `MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS` on the coordinator, and the
row appears on the worker's first heartbeat. The coordinator must have been
deployed at least once: it is the service the grant is written on, and it owns
the pool migrations.

1. Enrol the machine and mint its credential in one command:

   ```bash
   python3 scripts/pool_admin.py enroll --worker-id <id> --key-file gpu-worker-<id>.json
   ```

   That creates `gpu-worker-<id>@<project>.iam.gserviceaccount.com` if it does
   not exist, grants it `roles/run.invoker` on the coordinator and nothing else
   — no bucket, no Cloud SQL, no secrets — and writes a `0600` key file. The
   command's defaults are the production project, `us-west1`, and the service
   named by `tofu -chdir=infra output -raw coordinator_service_name`;
   `--project`, `--region`, and `--coordinator-service` override them. Omit
   `--key-file` when the machine already holds a working credential.
   Add the printed `enrollment` entry (house pool, `tenant_id: null`) to
   `MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS` for the coordinator.

2. Copy the key file to the machine and delete the local copy. It is that
   machine's only credential; an exported key is the Phase-1 bridge for machines
   outside GCP, isolated per machine so it is rotated or destroyed without
   touching any other worker.

3. Run the library's agent on the machine:

   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/gpu-worker-<id>.json
   export OGWP_WORKER_COORDINATOR_URL="$(tofu -chdir=infra output -raw coordinator_url)"
   export OGWP_WORKER_AUDIENCE="$(tofu -chdir=infra output -raw coordinator_audience)"
   export OGWP_WORKER_ID=<id>
   OGWP_WORKER_AUTH=google_oidc python -m outbound_gpu_worker_pool.agent_main
   ```

4. Prove the machine exposes nothing new by running the library's
   `deploy/agent/check-exposure.sh` on it, as the user the agent runs as: it
   exits non-zero if ComfyUI is bound off loopback or the agent is listening on
   any port.

`scripts/pool_admin.py` also has `drain` (finish running leases, take no new
ones), `activate`, and `list`. Those read and write registry rows, so they need
`MAGIC_VIDEO_EDITOR_DATABASE_URL` and a route into the VPC — run them from a
Cloud Run job or a bastion, since the pool database has no public IP. Every
registry operation goes through the library's registry API and issues no SQL of
its own.

### Revoking, retiring, and rotating

`python3 scripts/pool_admin.py revoke --worker-id <id> --iam` cuts a worker off
with `gcloud` alone: it removes the invoker binding, so the machine can no
longer reach the coordinator at all, and touches no database. That is the move
available from a laptop.

`python3 scripts/pool_admin.py revoke --worker-id <id>` is the softer lever:
it stops the coordinator from granting that worker any further lease immediately
and changes nothing in GCP, so a stolen key still authenticates and still gets
nothing. Like `drain`, `activate`, and `list`, it writes a registry row and so
needs a route into the VPC.

`python3 scripts/pool_admin.py retire --worker-id <id>` is for a machine that is
gone for good: it revokes the row first, then removes the invoker binding and
deletes the service account, so the credential itself stops existing. A machine
that never heartbeat has no row to revoke, which is not a failure — the command
says so and still destroys the identity.

`python3 scripts/pool_admin.py rotate-key --worker-id <id> --key-file <path>`
mints a replacement credential and prints the key ids it supersedes with the
exact `gcloud iam service-accounts keys delete` command for each. It deletes
nothing: run those commands once the machine is confirmed running on the new
key.

### Fail-closed defaults

Production behavior is unchanged. `MAGIC_VIDEO_EDITOR_WORKER_AUTH` defaults to
`none`, which leaves the whole pool unconfigured and the `/worker/v1` surface
unmounted; the GPU plugin stays `none`; and motion operations stay disabled. The
only capability this service publishes is `test.deterministic.echo.v1`, the
library's deterministic echo used to prove the round trip.

## Studio vertical slice

New animation projects start with artwork: upload one image, choose an editable
game-view template, declare the supplied facing when relevant, choose the requested
animations, and create a workspace. Setup saves choices without starting generation.
Single direction keeps the original view without assigning South; side-scrolling,
four-direction and eight-direction templates provide editable suggestions. Missing
references only matter for requested animations. A project can have no animations,
a partial set, or Idle and Death without Walk. Movie creation and legacy subject-first
projects retain their existing paths.

The legacy Subject workflow supports reference-driven generation and prepared-cutout
uploads through the same `SubjectRevision` contract. It accepts 1–6 PNG/JPEG/WebP
references, dispatches a versioned `GENERATE_SUBJECT` job, verifies exact input/output
lineage, and settles the canonical RGBA PNG into the manifest.

### Supplied character views and safe setup editing

One character project uses `single_view` (neutral Original view), `side_view`
(Left/Right), or `isometric` (a selected subset of compass headings). A stable view
identity is separate from its declared heading. Supply matching pixels; Studio
never invents or mirrors missing art. Identity art and opening references are
distinct roles, though the first uploaded image may establish both in one confirmation.
Replacing direction art preserves the identity anchor.

The workspace supports adding, removing and reassigning directions/actions,
replacing or clearing references, and changing game view. The server previews
exact impacts, then applies the reviewed setup atomically at the expected revision.
Content-bearing removals require confirmation; empty removals apply immediately
when safe. Changed references/headings keep old takes as stale history. Affected
active jobs block structural edits; late output cannot resurrect removed motions.
Historical manifest revisions and immutable releases retain their snapshots under
the existing retention policy. This does not provide undo or a new archive browser.

UI and MCP share setup schema version 1: capabilities, current setup, immutable
reference staging, preview and apply. New clients send `X-Studio-Setup-Version: 1`;
old clients receive an explicit unsupported-version response for versioned setup projects.
Legacy unassigned serialization remains compatible. See the
[delivery record](docs/plans/art-first-sprite-onboarding.delivery.md) for current
verification evidence.

The human UI provides generation, native idle/walk/attack loop proposals, manual
frame/timing edits, fixed clip-wide framing and pivots, review and export. Humans
judge the result. MCP exposes the same operations for an agent; the
[client skill](.agents/skills/magic-studios/SKILL.md) explains concise intent, worker
affinity, supplied-view setup, evidence and independent acceptance. Studio contains
no LLM judge, and technical findings or loop proposals are not visual acceptance.
Generic exports retain stable view/action identity and declared heading; legacy
target adapters that cannot represent view sets reject them explicitly.

### Targets

Magic Video Studio is a public product: nothing in its core knows the name of
a downstream game. Everything a game needs from the studio -- binding a
project to one of its entities, mapping the studio's neutral motion
vocabulary onto the game's own slots, and receiving a finished release -- goes
through one pluggable **target adapter** contract,
`magic_video_editor.targets.TargetAdapter`:

```python
class TargetAdapter(Protocol):
    descriptor: TargetDescriptor          # system id, display name, entity kinds, slots
    def slot_for(self, semantic_slot: str) -> TargetSlot | None: ...
    def export_files(self, package: NeutralPackage) -> Mapping[str, bytes]: ...
    async def deliver(self, delivery: DeliveryRequest) -> DeliveryResult: ...
    def parse_handoff(self, params: Mapping[str, str]) -> Handoff | None: ...
```

An "Example Engine" adapter lives in this repository only as the reference
implementation, at `magic_video_editor.targets.example`, discovered the same
way a private, third-party adapter would be.

- **Writing an adapter.** Implement `TargetAdapter`, declare a
  `TargetDescriptor` (your system's slug, display name, the entity kinds you
  accept, and your slots -- each a `{name, accepts, winner}` naming which
  neutral `semantic_slot` values map onto it and which one wins when more than
  one is present at once), and register a zero-argument factory under the
  `magic_video_editor.targets` entry-point group in your package's
  `pyproject.toml`:

  ```toml
  [project.entry-points."magic_video_editor.targets"]
  your-system = "your_package.adapter:create_adapter"
  ```

  `TargetRegistry.from_entry_points()` (the default the studio wires up)
  discovers every installed adapter this way; tests can also build a registry
  and `register()` an adapter explicitly, with no entry point required.

- **Config.** Each adapter's delivery credentials are generic:
  `MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_URL` and
  `MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_TOKEN` (`<SYSTEM>` is your descriptor's
  `system` slug, upper-cased). Both are required -- one alone leaves delivery
  disabled, and `GET /studio/targets` reports `delivery_configured: false` for
  an adapter missing either.

- **The contract suite.** `tests/targets/contract.py`'s
  `TargetAdapterContract` is a reusable pytest suite covering the whole
  contract -- `slot_for` resolution and collision handling, `export_files`
  determinism and archive-path safety, `deliver` idempotency and never
  raising, and `parse_handoff` never inventing a kind or slot it wasn't given.
  Subclass it, override `adapter()` to return a network-free instance of your
  own adapter, and pytest collects every `test_*` method against it:

  ```python
  from tests.targets.contract import TargetAdapterContract

  class TestMyAdapterContract(TargetAdapterContract):
      def adapter(self):
          return MyTargetAdapter(client=FakeHttpClient())
  ```

  `tests/targets/test_contract_example.py` runs the same suite against the
  Example Engine adapter; `tests/targets/test_contract_dummy.py` runs it
  against a minimal, deliberately different dummy adapter to prove the suite
  itself is adapter-agnostic.

#### Example Engine autobind

This legacy unassigned-project handoff uses target slots. It does not configure
the supplied-view animation matrix described above.

A character tagged in a target game can flow into the Studio and back with
no manual binding step, entirely through the target adapter contract above
-- demonstrated end to end here with the in-repo Example Engine adapter:

- **Tag at animation time (deep link).** A target links out to
  `/new?target=example&entity_kind=<character|prop>&entity_id=<id>&name=<display name>&slot=<idle|walk|attack>`
  (`slot` is optional). The web app resolves `target` against `GET
  /studio/targets` (no target name is hard-coded in web source), then the
  Studio looks up the owner's project already bound to that subject
  (`GET /studio/projects/for-external-subject`) and opens its Motions page
  with the requested motion selected; otherwise it creates a project already
  bound to the subject and pre-populated with that one preset (or the
  recommended six, when `slot` is omitted) in a single revision
  (`POST /studio/projects` with `external_subject` and `preset_motions`).
  `slot` maps onto the Studio's own preset names through the adapter's
  `TargetDescriptor.slots`.
- **Deliver at release time.** When a release's project carries a matching
  `external_subject` and delivery is configured, `create_release` exports
  the target's own profile (via `ReleaseExporter` + `TargetRegistry`),
  delivers it through the adapter's `deliver`, and records an append-only
  `ReleaseDelivery` on the manifest (`pending`/`delivered`/`failed`) --
  `GET`-ing a release exposes its full `deliveries` history, not just the
  latest attempt. Delivery is always best-effort: a failure never blocks or
  fails the release, and can be retried with
  `POST /studio/releases/{release_id}/deliveries/<system>`.

The Example Engine adapter
(`magic_video_editor.targets.example.adapter.ExampleTargetAdapter`) is
deliberately network-free -- it records what it received to an in-memory
list instead of calling out anywhere -- so a real adapter following the same
pattern is expected to follow the same retryable/terminal HTTP rigor the
studio's other HTTP plugins do, and to bound its importer's response size.

#### Installing a private target adapter

A downstream game's own adapter is a separate Python package, installed into
the API/worker image and discovered through the same
`magic_video_editor.targets` entry-point group as the in-repo Example Engine
adapter -- nothing in the studio core needs to change or know its name.

Pass one or more pip requirement strings (space-separated) as the
`TARGET_ADAPTER_PACKAGES` Docker build arg; each is installed after the
studio itself, in the same image:

```bash
docker build \
  --build-arg TARGET_ADAPTER_PACKAGES="git+ssh://git@github.com/<org>/<repo>.git#subdirectory=integrations/magic-video-target-<system>" \
  -t magic-video-editor .
```

Multiple adapters can be installed at once by separating requirement strings
with a space inside the same build arg. `cloudbuild.yaml` passes
`_TARGET_ADAPTER_PACKAGES` through to the image build as the same build arg,
so a Cloud Build substitution variable controls the same install at deploy
time without a Dockerfile change.

At runtime, each installed adapter's delivery credentials are supplied
generically: `MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_URL` and
`MAGIC_VIDEO_EDITOR_TARGET_<SYSTEM>_TOKEN`, where `<SYSTEM>` is the adapter's
own `TargetDescriptor.system`, upper-cased. Both are required for delivery to
be attempted; either alone leaves the adapter installed but not deliverable.

### Local development

Use Python 3.13 and Node 22 or newer. Start the API and worker as distinct
processes so their runtime roles match production:

```bash
MAGIC_VIDEO_EDITOR_SERVICE_ROLE=api uv run uvicorn magic_video_editor.app:app --port 8080
MAGIC_VIDEO_EDITOR_SERVICE_ROLE=worker uv run uvicorn magic_video_editor.app:app --port 8081
cd web
npm ci
STUDIO_API_ORIGIN=http://127.0.0.1:8080 npm run dev
```

The PostgreSQL adapter applies numbered migrations from
`src/magic_video_editor/migrations` during API startup. Configure database, GCS,
Cloud Tasks, signing identity, and worker URL with the
`MAGIC_VIDEO_EDITOR_*` variables used in `infra/main.tf`. The worker additionally
receives the GPU endpoint and audience; the API identity has no GPU permission.

Run the verification suites with:

```bash
uv run --extra test pytest -q
cd web
npm test -- --run
npm run lint
npm run build
npm run e2e
```

For the complete local vertical flow, Playwright starts
`magic_video_editor.local_studio:app` on port 8787 and Vite on port 4173. That
composition uses the real FastAPI routes, manifest/store transitions, job ledger,
worker execution function, quality policies, release signer, and exporter. Only
the external GPU and object store are replaced by deterministic in-memory
adapters, exposed through real HTTP upload/download routes. Run it directly with:

```bash
cd web
npm run e2e -- studio-flow.spec.ts
```

The browser flow creates a fresh project, uploads the checked-in RGBA PNG,
observes independently running motions, and validates the generated ZIP plus a
byte-identical repeated export. Browser `performance.mark`/`measure` records API,
generation, cached-navigation, and preview-speed latency separately. Cached
navigation has a 500 ms budget; API project creation has a 750 ms local budget;
the local-only preview speed control must render by the next animation frame.
Jobs remain visible as local progress and never replace the page with an overlay.

### Observability

Sentry is optional and env-only: this repo is public, so no DSN, token, or org
name lives in source anywhere. Both the service and the web app are documented
no-ops until a DSN is supplied.

**Two separate Sentry projects** are expected — one for the service (API +
worker), one for the browser build — because they ship independent releases
and carry different scrubbing/sampling needs.

Service (`src/magic_video_editor/monitoring.py`, wired in `create_app`):

| Variable | Default | Meaning |
|---|---|---|
| `SENTRY_DSN` | empty (disabled) | Service-project DSN. `configure_sentry` is a no-op without it. |
| `SENTRY_ENVIRONMENT` | `development` | Sentry environment tag. |
| `SENTRY_RELEASE` | `MAGIC_VIDEO_EDITOR_RELEASE`, else unset | Release identifier attached to every event. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.05` | Fraction of requests traced. |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0` | Fraction of traced requests profiled. |

FastAPI, Starlette, httpx, and asyncio integrations are enabled; `send_default_pii`
is always `False`. A `before_send` hook scrubs `Authorization`,
`X-Studio-Actor-Id`, and `X-Studio-Release-Id` header values and any request
query/body field literally named `token` before an event leaves the process.
Every event is tagged `service_role` (`api`/`worker`/`coordinator`). A job
execution failure additionally captures `job_id`, `operation`, `project_id`,
and a `retryable`/`terminal` classification — additively, alongside (never
instead of) the existing `runtime.jobs.fail`/`release`/`complete` recording.
`GET /health` reports whether Sentry is active under `observability.sentry`.

Web (`web/src/monitoring.ts`, called once from `web/src/main.tsx`):

| Variable | Default | Meaning |
|---|---|---|
| `VITE_SENTRY_DSN` | empty (disabled) | Web-project DSN, embedded into the static build. |
| `VITE_SENTRY_ENVIRONMENT` | `development` | Sentry environment tag. |
| `VITE_APP_RELEASE` | unset | Release identifier attached to every event. |

Browser tracing samples at `0.05` and only propagates trace headers to
same-origin `/studio` requests; session replay is off. The whole app is
wrapped in a `Sentry.ErrorBoundary` whose fallback reuses the existing
"Studio unavailable" recovery screen (`StudioUnavailable` in `web/src/app/App.tsx`),
so a reported crash and an unreported one look the same to the operator.

**Visitor analytics** (`web/src/analytics.ts`, called once from
`web/src/main.tsx`) is Cloudflare Web Analytics, which is free and cookieless.
`magicstudios.ai` resolves to Google directly (its Cloudflare DNS records are
not proxied), so Cloudflare's edge sees no traffic, and the site loads the JS
beacon from `static.cloudflareinsights.com` instead. The beacon stores nothing in the browser,
Cloudflare does not log query strings, and it counts single-page route changes
itself. It reports page views, referrers, countries, browsers, devices and Core
Web Vitals. It has no custom events, so clicks such as Request access or a
download are not counted.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_CF_BEACON_TOKEN` | empty (disabled) | Web Analytics site token. With no token, no beacon is added in local development, Vitest or CI. |

The token comes from Cloudflare → Web Analytics → Add a site (manual JS
snippet). It is public in the served page, so the `Deploy` workflow supplies it
from the `CF_BEACON_TOKEN` repository variable rather than a secret.
`cloudbuild.yaml` takes it as `_VITE_CF_BEACON_TOKEN`. The SPA sends no
Content-Security-Policy, so the beacon needs no allowlist entry. If one is
added later, allow `static.cloudflareinsights.com` (script) and
`cloudflareinsights.com` (connect).

**Site media** (`web/src/siteMedia.ts`): the welcome page's demo videos, sprite
sheets and captions, and the `/wyrm` teaser, are not in Git.

| Variable | Default | Meaning |
|---|---|---|
| `VITE_SITE_MEDIA_BASE` | unset (no media) | Prefix of the staged welcome media, such as `/assets/site-media/1`. Unset, the welcome page lists its four modes and shows only the agent example, with no media demos. |

The `Deploy` workflow reads the base from `scripts/site_media.json`, and after
the build `scripts/stage_site_media.py` downloads the pinned private release,
checks every size and SHA-256, and copies the files into `web/dist`. Never
replace a release asset: `assets/` is served as immutable, so publish
`site-media-N+1` and bump `base` instead.

**Setting the DSNs**: create two Sentry projects (service, web) and put each
DSN in the deploy path that already handles other secrets for that
deployable — never in a committed file:

- Service: Terraform variables `sentry_backend_dsn` (sensitive, default empty)
  populates the `magic-video-editor-sentry-dsn` Secret Manager secret, mounted
  as `SENTRY_DSN` on both the `magic-video-studio-api` and
  `magic-video-editor-worker` Cloud Run services, alongside
  `SENTRY_ENVIRONMENT=production` and `SENTRY_RELEASE` set from the deployed
  image reference. `scripts/deploy.sh` forwards it from the `SENTRY_BACKEND_DSN`
  environment variable; the `Deploy` workflow supplies that from the
  `SENTRY_BACKEND_DSN` GitHub Actions secret.
- Web: the `VITE_SENTRY_DSN` build-time env var, supplied by the
  `SENTRY_WEB_DSN` GitHub Actions secret in `.github/workflows/deploy.yml`, or
  by the `_VITE_SENTRY_DSN` Cloud Build substitution in `cloudbuild.yaml` for
  the Docker-based `web-build` target.

**Source-map upload** only runs when `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and
`SENTRY_PROJECT` are all present at build time (`web/vite.config.ts`); it is
never required locally, in `npm test`/`npm run build` in CI, or in the plain
`docker build --tag magic-video-editor:release-gate .` release-gate step —
all of those omit the trio and the build succeeds with plain, unmapped output.

### Export contract

An export is a reproducible derivative of an immutable release. The downloaded
ZIP contains transparent PNG frames plus `manifest.json`. The manifest pins its
format/profile version and release digest, orders frames per motion, and preserves
canvas size, frames-per-second/frame duration, loop policy, and frame events.
Re-requesting the same profile for the same release returns the same artifact;
an integrity mismatch fails closed instead of overwriting it.

### Operators and extensions

`MAGIC_VIDEO_EDITOR_ADMIN_EMAILS` names the operators (`studio/operators.py`). They
see the full worker registry, including offline machines, and the pool library's
global status views (`GET /pool/queue` and its unscoped worker listing), and may
submit untenanted house work through `POST /pool/jobs`. Everyone else sees their own
machines plus the house machines through Studio's tenant-scoped `GET /pool/workers`.
Membership is resolved from the stored, verified Studio identity.

Deployment-specific features are extensions (`magic_video_editor/extensions.py`):
packages registered in the `magic_video_editor.extensions` entry-point group and
loaded only when `MAGIC_VIDEO_EDITOR_EXTENSIONS` names them. A named extension that
is not installed stops the service from starting. An extension can replace the
operator access (for example to give operators a shared compute tenant), add an
"invited" check to sign-in and the account tier, wrap the pool job store, and add
stores, routes, API reference entries, account erasers and retention purges.
Studio builds, runs and passes its tests with no extension loaded.

The hosted magicstudios.ai service loads its private `hosted` extension: the admin
GPU cluster (a shared operator tenant that admin-owned and house workers lease
from), access requests and invitations, and tips through Stripe Checkout. The image
build installs it with `--build-arg EXTENSION_PATHS=hosted`, which also sets
`MAGIC_VIDEO_EDITOR_EXTENSIONS=hosted`.

### CPU movie workers

Attached machines can process `movie.ffmpeg.process.v3` jobs (final-frame extraction,
video probing, trimmed clip stitching with dissolves and soundtrack mixing) without a GPU. The
attach scripts download the standalone CPU plugin alongside the runner. Install
`ffmpeg` and `ffprobe` on PATH before starting the worker; the runner advertises
CPU processing only when both are present. Existing installations need the updated
`/studio/worker.py` and `/studio/cpu-worker.py` (saved as `cpu_worker.py` beside
`worker.py`) and a restart. This does not change their enrollment or credentials.
The machines page reports CPU movie processing when advertised. Version 1 and 2
workers remain compatible with old jobs, but new jobs require version 3 (cuts fill
the 768×432 frame; imported clips of another shape are fitted and padded); the
service uses local processing until an updated worker is available. Assembled cuts
are named `<cut digest>-<renderer>.mp4`, so a renderer change renders an unchanged
cut again instead of reusing the old file; the cut digest that release checks is
unaffected.

Movie reconciliation submits idempotent CPU jobs when a compatible online worker
can lease that movie's tenant. Admin machines and house workers share admin CPU
jobs through the same cluster policy as GPU work. With no eligible CPU worker,
processing stays local; once queued, a CPU job remains in the durable pool and is
never duplicated locally. Large cuts beyond the pool's 16 distinct-input limit
also remain local. Import validation and audio probing stay on the service.
CPU execution uses two FFmpeg threads, bounded media sizes and subprocess timeouts,
allowlisted payloads and local files only. The agent's normal concurrency limit
applies to CPU and GPU jobs together; this does not add simultaneous CPU/GPU slots
on one machine. Signed transfers, immutable publication and completion fencing
are unchanged.

## Product launch scope

Personal BYOG is free within account storage and concurrency allowances. Owners
without a GPU can instead bring their own Higgsfield key (below). Optional
prepaid hosted generation is planned; checkout is not enabled. Public GPU sharing,
contribution credits and memberships are outside launch scope. One-time tips
(from the hosted extension) are the only payment, and they buy nothing. Existing
admin cluster scheduling remains internal. The public `/about` page (its Cost section;
`/cost` and `/pricing` redirect there) and `/storage-policy` describe actual availability. See [launch readiness](docs/launch.md).

Anyone can sign up, with Google or an email and password (Auth0's
`Username-Password-Authentication` database connection). The front page's Sign up
opens Auth0's hosted page on its sign-up form (`/studio/auth/login?signup=true`). A new
password account must follow its verification link before it can sign in; until then
the callback sends it to `/login?error=verify_email`. This is the public-signup switch
(`PUBLIC_SIGNUP_ENABLED` repository variable, `MAGIC_VIDEO_EDITOR_PUBLIC_SIGNUP_ENABLED`).
Self-registered accounts run only on their own machines or Higgsfield key, get no
house director, a 5 GiB default storage cap, and no MiniMax H3 unless
`PUBLIC_H3_ENABLED` is also set; see [launch readiness](docs/launch.md#public-signup).

Only invited accounts use the shared machines (house GPUs, MiniMax H3, the house
director, the 50 GiB default cap), and an account is invited only through the explicit
allowlist (`MAGIC_VIDEO_EDITOR_ALLOWED_EMAILS`, `MAGIC_VIDEO_EDITOR_ALLOWED_EMAIL_DOMAINS`)
or `MAGIC_VIDEO_EDITOR_ADMIN_EMAILS`. There is no way to ask for or grant that access in
the app.


### Bring your own Higgsfield key

Owners can save a Higgsfield API key on the Machines page and choose
"Run on: Higgsfield" in the motion workspace (or pass discovered settings via
MCP). The motion's opening frame, compiled prompt and duration go to the owner's
Higgsfield account, which bills them directly; Magic Studios adds no fee. Offered
models are Kling 3.0 Standard and Pro and Seedance 2.0
(`plugins/higgsfield.py`); none has been benchmarked here.

- Keys are set and removed only from a browser session, encrypted with
  `MAGIC_VIDEO_EDITOR_PROVIDER_CREDENTIAL_SECRET` (Secret Manager
  `magic-video-editor-provider-credential-secret`, mounted on the Studio API
  only) and stored in `studio_provider_credentials`. Without that secret the
  feature reports itself unavailable. Keys never enter job payloads, provenance
  or logs, and are sent only to `api.higgsfield.ai`.
- A Higgsfield job is a normal `video_jobs` row that is never enqueued to Cloud
  Tasks or the GPU pool. `studio_provider_requests` reserves each job before the
  provider call, so a job is submitted at most once; an unconfirmed submission
  fails after ten minutes instead of being replayed. The operations/reconcile
  reads poll Higgsfield, copy the result into immutable storage and complete the
  row with provider-reported provenance (`completion_path: "higgsfield_api"`).
- Settings never cross routes: a Higgsfield request may not carry worker or
  recipe fields, and GPU requests never fall back to Higgsfield.
- Desktop and local modes do not offer it; they have no provider-key store.

Enrolled machine reboot behavior, ComfyUI startup recovery, FFmpeg discovery,
WSL sign-in requirements, and updating existing workers without re-pairing are
covered in [the hosted machine setup guide](docs/hosted-machine-setup.md#restart-recovery-and-existing-workers).

## Licence

Magic Studios is released under the [MIT License](LICENSE). The models it drives
are not included and each keeps its own licence. In particular, the MiniMax H3
Community License excludes the United States, the European Union, the United
Kingdom and the Republic of Korea. See [NOTICE](NOTICE). To contribute, read
[CONTRIBUTING.md](CONTRIBUTING.md); to report a vulnerability, follow
[SECURITY.md](SECURITY.md).
