# Hosted machine defaults and enrollment

This follows PR #214's local prototype. The worker and hosted coordinator must
run the matching new pool dependency before using these profiles. A merge or a
successful local test is not proof of deployment or hosted enrollment.

## This RTX 4080 desktop

Use the **Linux/WSL agent**, connected to the existing **Windows portable ComfyUI**
at `http://127.0.0.1:8189`. WSL does not need its own ComfyUI or model copies.
Windows-native enrollment is also supported through `attach.ps1`; choose one
agent, not both. The configured GPU recipe is the same regardless of agent OS.

1. Deploy the reviewed app/coordinator image and its bundled worker wheel through
   the repository's saved-plan deployment workflow. Check production verification.
2. Start Windows ComfyUI using `C:\AI\start-windows-comfy.ps1 -Attention sage`.
   Models remain in `C:\AI\Models`. No local model download is needed on this box.
3. On Magic Studios' **Your machines → Add a machine**, select **macOS or Linux**,
   **H3 16GB — W4A8, LightX2V 8 steps**, and URL `http://127.0.0.1:8189`.
4. Paste the generated command into WSL. It installs only the lightweight worker
   in `~/.magic-video-worker`, checks ComfyUI before spending the pairing code,
   pairs through the existing authenticated enrollment, and persists its profile
   in `~/.config/magic-video-studio/worker.env`. Do not publish that credential file.
5. Check `systemctl --user status magic-video-worker` and the Machines page. It
   should show the W4A8/eight-step configuration, rather than only CPU capability.
6. Through hosted MCP, call `get_generation_config`. Confirm the selected worker,
   16,000 MB minimum, bounded capability, eight steps, 768p adapter and Kitchen.
   Pass its returned `settings` unchanged to `generate_motions` for an explicit
   binding, or omit settings and let the controller resolve the machine default.
7. Generate in a new test project. Confirm completed operation/take provenance:
   executing worker ID, requested versus resolved settings, model/adapter recipe,
   runtime/configuration versions, worker-attested effective inputs and output hash.
   Then extract a walk loop, inspect native frames and download an alpha export.
   Preserve the original selected Walk/Attack assets.

The shell command has this shape (use the actual one-time code from the UI):

```sh
curl -fsSL https://magicstudios.ai/studio/attach.sh | \
  MVS_WORKER_PROFILE=h3-16gb-lightx8 \
  OGWP_COMFY_URL=http://127.0.0.1:8189 sh -s -- YOUR_PAIRING_CODE
```

The agent makes outbound connections. Do not expose ComfyUI, the local UI or MCP
to the LAN/Internet. Keep Mac access to the local server on authenticated SSH
forwarding. Hosted account authentication and worker pairing remain separate.

### Starting ComfyUI after reboot

The portable runtime must be running before the worker can advertise a profile.
An optional operator-owned `OGWP_COMFY_START_COMMAND` can start it when unavailable;
set this environment variable before attaching, and setup will persist it. It is
executed locally as arguments, never received from a caller or job payload.
For this existing desktop, the command may invoke the installed Windows
PowerShell launcher, with its path quoted as one argument and `-Attention sage`.
Without a start command, the worker retries until ComfyUI is started manually.
Do not run local and hosted generation simultaneously while validating enrollment.

## Profiles and existing workers

| Explicit profile | Recipe | Bounds | Runtime verification |
| --- | --- | --- | --- |
| `h3-16gb-lightx8` | W4A8 H3, NVFP4/AWQ encoder, INT8 video VAE, LightX2V 8-step v1.0 **768p** at strength 1; res_multistep/simple; H3 Kitchen over global Sage | ≥16,000 MB, dimensions 256–512, ≤4 requested seconds, 24 FPS, **exactly 8 steps**, no audio decode | Models/nodes, CUDA 13 PyTorch, Sage/dynamic-VRAM/fast-disk/disabled-pinned-memory flags, reserve 0 |
| `h3-24gb-standard` | Existing INT8 H3 graph, NVFP4/AWQ encoder, FP16 video VAE, no Turbo adapter; **20 default steps**, res_multistep/simple; audio decode retained | ≥24,000 MB; existing graph bounds | Required models/nodes and GPU memory; actual process attention/runtime reported rather than prescribing 4080 flags |
| `flux2-klein-subject-12gb` | Subject images only: the unchanged Klein v2 graph (fp8 Klein base 4B, qwen_3_4b BF16, flux2-vae) under `image.flux2_klein.subject.12gb.v1` | ≥12,000 MB, 1024², **exactly 20 steps**, ≤3 references | Models/nodes and GPU memory; refuses `--gpu-only`/`--highvram`. Evidence: 4090 held to 12 GB free, not a physical 12 GB card |

These are deployment recipes, not quality tiers. The installer verifies a
pre-provisioned ComfyUI; it does not install large model weights or attention
packages. Other machines need the exact files listed by `describe_profile` in the
pinned pool package. Runtime packages and Windows launchers on this desktop are
outside Git; preserve them when moving/rebuilding the worker.

An explicit profile advertises only its H3 motion capability plus the runner's
existing CPU movie processing. It does not claim Flux/dialogue capabilities merely
because their templates ship in the package. Standard generic workers and the
standard 24GB capability/minimum are unchanged. Legacy standard workers without
recipe fingerprints remain on their existing request path and are clearly
reported as unverified in discovery; migrate them to the explicit standard profile
for configuration binding. Unavailable 16GB profiles never masquerade as standard
24GB workers.

### Last-frame (closed-loop) support

Turnarounds use a closed loop: the same prepared frame goes into the H3
template's first and last slots. Only the standard profile can claim this, through
a separate runtime-version key, `magic_feature_last_frame: "1"`, from pool
`e6691d7` onward. The worker reports it only when the pinned template wires the
`last_frame` slot and the live `MiniMaxH3ImageToVideo` node lists `last_frame`
among its inputs. The key changes neither the recipe version nor the configuration
fingerprint, so a worker without it keeps leasing every ordinary standard job.
Only closed-loop jobs require it, both at lease and at completion attestation.
The 16 GB profile never reports it.

A worker reports the key once its installed pool package is at least `e6691d7`
and it restarts (the runner re-runs the profile check on every start). A worker
attached after the deployment gets that package from the studio's worker
package. **An existing worker does not**: the credential-preserving updater
below refreshes scripts, not the pool dependency. Such a worker keeps serving
ordinary jobs, and discovery shows it without last-frame support, until its pool
package is reinstalled. A server deployment updates no worker. The house 4090's
hardened agent is on legacy standard registration (no recipe version), so it
cannot take closed-loop work until its image is rebuilt from the new pin and it
registers the standard profile.

## Selection, binding and failures

The controller scopes discovery to the job owner's enrolled tenant, or the house
pool when the owner has no enrolled machines. It selects an available verified
worker (online before busy, then worker ID). Existing admin-cluster policy still
applies. Callers may request an authorized `worker_id`; this never bypasses tenant,
capability, VRAM or recipe checks. Offline/draining/stale machines are unavailable.

`GET /studio/generation-config?worker_id=...`, also exposed by MCP
`get_generation_config(worker_id=...)`, returns the selected machine, recipe,
configuration version and complete default `settings`. There is no quality menu.
Explicit incompatible steps/bounds or a stale preflight are rejected before a job
is submitted. Omitted steps become eight or twenty before payload/provenance are
recorded. The UI loads the same defaults; B's steps are locked.

The coordinator persists worker ID and recipe/configuration requirements with the
job. Memory and PostgreSQL stores enforce them atomically on every lease/retry.
The executor rechecks ComfyUI before execution, and completion checks the reported
recipe/configuration and effective input values. A failed bound submission never
falls back to Cloud Tasks. A removed/offline worker can leave a bound job waiting;
cancel and resolve a new request to select another machine, rather than silently
changing worker or recipe. There is deliberately no automatic cross-recipe reroute.

Provenance distinguishes requested settings, resolved recipe and worker-attested
execution. It verifies protocol identity/digests; it is not a cryptographic proof
of model file contents or a new quality guarantee. No real 24GB benchmark is claimed.

## Repeatable local verification

```sh
uv run --extra test python scripts/verify_machine_profile.py \
  --comfy-url http://127.0.0.1:8189 \
  --source /absolute/path/to/prepared-character.png \
  --output-dir /tmp/magic-profile-check
```

The script requires an idle ComfyUI queue. It uses the actual worker agent,
coordinator routes, authenticated lease, immutable transfer and completion with
isolated in-memory jobs/assets. It does not enroll a hosted worker or modify a
Studio project. `--preflight-only` checks readiness without generating.

Local validation on this RTX 4080 produced a 512×512, 107-frame, 24 FPS clip with
no audio; ComfyUI reported 25.25 seconds for the initial protocol render. The
original asset selection was untouched. Live hosted pairing and Mac-side testing
remain acceptance steps after deployment, not implied by this local evidence.

## Restart recovery and existing workers

Enrollment persists credentials; do not enroll again after reboot or to update.
The worker starts via systemd on Linux/WSL and a sign-in task on Windows. WSL's
systemd service does **not** boot WSL: configure Windows to start the distro at
sign-in (and keep it alive), enable user lingering, or use your managed boot
service. Sign-in startup does not run before a Windows user signs in.

For a separate ComfyUI installation, set `OGWP_COMFY_START_COMMAND` before
pairing. This is an executable plus arguments (POSIX/shlex quoting, even on
Windows), not a shell script expression. It must launch independently, return
success, and preserve the selected profile's flags. Startup preflight launches
it only if ComfyUI is unavailable, then waits for readiness and verifies the
recipe. Requests do not launch runtimes themselves: worker startup recovery
makes the capability available first. Remote ComfyUI services must have their
own startup management; never launch a local GPU for a remote URL.

For the verified 16GB desktop layout, setup discovers
`C:\AI\start-windows-comfy.ps1` and Windows PowerShell, with the loopback URL
`http://127.0.0.1:8189`, and configures `-Attention sage` recovery automatically.
Other layouts and the standard 24GB profile require an explicit launch command;
setup does not guess attention flags or download models. A terminal-only launcher
that dies when the terminal closes is unsuitable.

FFmpeg runs in the **worker OS**, independently of ComfyUI's OS and the hosted
backend. Set `MVS_FFMPEG_BIN` to a directory containing executable `ffmpeg` and
`ffprobe` before enrollment when they are not on the service PATH. Setup also
recognizes the existing Linux `~/.local/opt/ffmpeg-ubuntu/bin` installation.
The runner adds this directory to PATH before registering CPU processing. If
neither binary is available, GPU generation can still work but this worker does
not advertise CPU movie processing. No download or system-wide install is implied.

Existing workers download their runner at enrollment; deployment alone does not
update them. After this change is deployed, update a Linux/WSL systemd worker:

```sh
# Stop gracefully: current jobs drain before the service exits.
systemctl --user stop magic-video-worker
curl -fsSL https://magicstudios.ai/studio/worker-update.py -o ~/.magic-video-worker/worker-update.py
~/.magic-video-worker/venv/bin/python ~/.magic-video-worker/worker-update.py
systemctl --user start magic-video-worker
journalctl --user -u magic-video-worker -n 20 --no-pager
```

The updater refuses a running service (including `--rollback`) and never starts
or stops one. It stages and parses the scripts, then brings the pool package to
the build this Studio ships:

1. It asks `/studio/workers/package/manifest` for the build, presenting the
   machine's own credential from `worker.env`. No pairing code is spent.
2. Every pool build is version 0.1.0, so builds are told apart by the wheel's
   sha256, recorded in `~/.magic-video-worker/pool-package.json` (written by
   attach and by each update). A matching record downloads nothing. A worker
   attached before this record existed installs the current build once.
3. Otherwise it downloads the wheel from `/studio/workers/package/update`,
   checks size, name and digest, and backs up the installed package's files to
   `~/.magic-video-worker/rollback/`.
4. Before changing anything, it resolves the new wheel with the `agent` and
   `comfy` extras offline against what is installed (`uv pip install --dry-run
   --no-index`). A build that needs a dependency the machine lacks is refused
   with nothing changed; re-attach the machine to install it. Otherwise it
   reinstalls only the pool package (`--no-deps`) and compares every installed
   file with the wheel's RECORD.
5. In a fresh process it imports the agent and runs the profile check that
   decides what the worker advertises. It prints the commit, the digest and the
   optional features found, such as `magic_feature_last_frame` on a standard
   profile whose ComfyUI accepts a last frame.

The scripts, the configuration and `pool-package.json` are written only after
the new package passes. Any failure after the backup, including one while
replacing a script, writing the configuration or recording the build, restores
the previous package files (clearing their bytecode caches), scripts,
configuration and `pool-package.json`, and exits non-zero with the worker still
stopped. Ctrl-C or SIGTERM during the update restores the same way. An installed
package whose file list (`RECORD`) cannot be read is never replaced: a failed
update could not restore it.

`~/.magic-video-worker/rollback/` keeps only the backup `--rollback` can use,
the build before the last successful update; older ones are removed. Each
backup also records the files of the build installed over it, so `--rollback`
removes that build's files even if its `RECORD` was damaged. With neither, the
rollback is refused without changing anything. Only `--rollback` or no argument is accepted. Start
it again to keep running the previous build. To return to the build before the
last successful update, run the updater with `--rollback`. That restores only the
pool package, so run it with the worker stopped and start the worker afterwards.
The renewing credential is preserved. A revoked or expired credential is refused
before anything changes.

The updater is for attached script workers. A worker deployed from the pool's
hardened container profile (Google OIDC, immutable images) updates by rebuilding
its images from the new pin; this updater does not apply to it.

Windows-native operators must wait for jobs to finish, stop
`MagicVideoStudioWorker`, download the same updater, run it with
`.magic-video-worker\venv\Scripts\python.exe`, then start the task.
Do not force-stop an active Windows job. Manually launched workers must also be
stopped before updating; the updater can only detect the standard service/task.
